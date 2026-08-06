"""Doc to agent — send a plan you wrote in Notes straight to the model.

The workflow this replaces is copy-and-paste: you think something through in a
note, then re-type or paste it into the chat box, losing the note's structure
and any sense of which files it was about. Here the note *is* the prompt.

Three inline references make a note self-describing. Everything after ``@/`` is
slash-delimited, and every form is plain text so it survives a round trip
through the markdown editor untouched:

* ``@/file/src/router.py`` — cite a file. Its contents are read at send time and
  attached as context, so the model sees the actual file, not your description
  of it. Quote paths with spaces: ``@/file/"my paper.pdf"``.
* ``@/model/openai/some-model`` — pick the provider and model for this note. A
  research note can name a frontier model while a scratch note stays on-device.
* ``@/to/research/deep`` — say where the note goes. A note is not always a chat
  turn: a question you have been circling for a week wants Carrot Research, and
  a checklist of things to do on a website wants Carrot Agent. Writing the
  destination into the note means the note stays the whole instruction, which
  is the point of the feature — you should not have to remember which button to
  press for a document you wrote three days ago.

Naming the provider is mandatory rather than inferred, which is what makes the
model form unambiguous: the segment after ``model/`` is always the provider, so
everything left over is the model id no matter how many slashes it contains.

Cited files follow the note to wherever it goes. Sent to Research they are
*seeded as evidence*, so the papers you already have are read against every
sub-question and cited by number alongside anything found on the web — which is
the difference between "research this" and "research this, starting from what I
have already collected".

Citations may only reach two places, both of which the user already pointed
Carrot at deliberately: the agent workspace, and the indexed document folders.
Anything else is refused and reported as a warning rather than silently
dropped — a plan that cites a file it cannot read should say so.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import agent_tools, indexer, providers as providers_mod, router as router_mod

REFERENCE_PATTERN = re.compile(r'@/(file|model|to)/(?:"([^"\n]+)"|([^\s]+))')

# ===== Destinations =====
#
# Each destination names the pipeline a note is sent to and what its optional
# second segment means there. `default` is used when the note names a
# destination but not an option — `@/to/research` is a complete instruction.

DESTINATION_CHAT = "chat"
DESTINATION_RESEARCH = "research"
DESTINATION_AGENT = "agent"

DESTINATIONS: Dict[str, Dict[str, Any]] = {
    DESTINATION_CHAT: {
        "label": "Chat",
        "option_label": "",
        "options": [],
        "default": "",
        "help": "An ordinary chat turn, with the note as the message.",
    },
    DESTINATION_RESEARCH: {
        "label": "Research",
        "option_label": "depth",
        "options": ["quick", "standard", "deep"],
        "default": "standard",
        "help": "Carrot Research. The note becomes the question; cited files become evidence.",
    },
    DESTINATION_AGENT: {
        "label": "Agent",
        "option_label": "surface",
        "options": ["browser", "desktop", "both"],
        "default": "browser",
        "help": "Carrot Agent. The note becomes the task; cited files become background.",
    },
}

# Which routing task a destination implies, so an unpinned note lands on the
# model the user assigned to that kind of work.
DESTINATION_TASKS = {
    DESTINATION_CHAT: router_mod.TASK_CHAT,
    DESTINATION_RESEARCH: router_mod.TASK_RESEARCH,
    DESTINATION_AGENT: router_mod.TASK_AGENT,
}

# "local" reads better than the daemon's name in a document, and is what the
# picker offers, but the underlying provider id is still ollama.
PROVIDER_ALIASES = {"local": providers_mod.LOCAL_PROVIDER}

# Punctuation that is almost certainly sentence structure rather than part of a
# path or model name. ':' is excluded — Ollama tags like `gemma4:e4b` need it.
TRAILING_PUNCTUATION = '.,;!?)]}'

# Citation budgets. A note that cites ten files should still produce a prompt
# the model can actually read.
MAX_FILE_CHARS = 20000
MAX_TOTAL_CHARS = 80000
MAX_FILES = 10

# Candidate lists for the '@' menu are cached briefly — the menu opens on a
# keystroke and should not wait on a remote /models call every time.
CANDIDATE_TTL_SECONDS = 120
_candidate_cache: Dict[str, Tuple[float, Any]] = {}


@dataclass
class Reference:
    """One ``@/file/`` or ``@/model/`` mention found in a document."""

    kind: str
    value: str
    raw: str
    ok: bool = False
    detail: str = ""
    path: str = ""
    chars: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "value": self.value, "raw": self.raw,
            "ok": self.ok, "detail": self.detail, "path": self.path, "chars": self.chars,
        }


@dataclass
class ResolvedDoc:
    """A document turned into everything one send needs, wherever it goes."""

    prompt: str
    context: str = ""
    route: Optional[router_mod.Route] = None
    references: List[Reference] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    destination: str = DESTINATION_CHAT
    option: str = ""

    @property
    def files(self) -> List[Reference]:
        """The citations that actually resolved, in the order they appeared."""
        return [r for r in self.references if r.kind == "file" and r.ok and r.path]

    def seed_sources(self) -> List[Dict[str, str]]:
        """Cited files as evidence rows Carrot Research can cite by number."""
        seeds = []
        for reference in self.files:
            try:
                seeds.append({
                    "kind": "document",
                    "locator": reference.path,
                    "title": os.path.basename(reference.path) or reference.value,
                    "snippet": "",
                    "content": read_citation(reference.path),
                })
            except Exception:
                continue
        return seeds

    def as_dict(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt,
            "context": self.context,
            "route": self.route.as_dict() if self.route else None,
            "references": [r.as_dict() for r in self.references],
            "warnings": self.warnings,
            "destination": self.destination,
            "option": self.option,
        }


# ===== Parsing =====

def parse_references(text: str) -> List[Reference]:
    """Every ``@/file/``/``@/model/`` mention in a document, in order."""
    found: List[Reference] = []
    for match in REFERENCE_PATTERN.finditer(text or ""):
        kind = match.group(1)
        quoted, bare = match.group(2), match.group(3)
        value = quoted if quoted is not None else (bare or "").rstrip(TRAILING_PUNCTUATION)
        if not value:
            continue
        found.append(Reference(kind=kind, value=value, raw=match.group(0)))
    return found


# ===== File citations =====

def _index_roots() -> List[str]:
    return [os.path.abspath(d) for d in indexer.index_dirs()]


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)

def citable_path(value: str) -> str:
    """Resolve a citation to a real path, or raise with a UI-safe reason.

    A citation is a read of the user's disk, so it is confined to the two
    places they already opted in: the agent workspace and the indexed folders.
    """
    workspace = agent_tools.workspace_root()

    # Workspace-relative first — that is what a plan about your own code means.
    if not os.path.isabs(value):
        try:
            candidate = agent_tools.resolve(value)
        except PermissionError as exc:
            raise ValueError(str(exc)) from exc
        if os.path.isfile(candidate):
            return candidate

    absolute = os.path.abspath(os.path.expanduser(value))
    roots = [workspace] + _index_roots()
    if os.path.isfile(absolute) and any(_within(absolute, root) for root in roots):
        return absolute

    if os.path.isfile(absolute):
        raise ValueError(
            "outside the workspace and indexed folders — add its folder in Settings to cite it"
        )
    raise ValueError("no such file in the workspace or indexed folders")


def read_citation(path: str) -> str:
    """The text of a cited file, truncated to the per-file budget."""
    text = indexer.extract_text(path)
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + f"\n… [truncated at {MAX_FILE_CHARS} characters]"
    return text


# ===== Model citations =====

def provider_id_for(name: str) -> str:
    """Resolve a name written in a document to a real provider id."""
    name = (name or "").lower()
    return PROVIDER_ALIASES.get(name, name)


def resolve_model_reference(value: str) -> Tuple[str, str]:
    """Split ``provider/model`` into its parts.

    The provider is always the first segment, so a model id with slashes of its
    own — ``openrouter/meta-llama/llama-3-70b`` — needs no special handling.
    A value with no provider returns an empty one, which the caller reports.
    """
    head, separator, tail = value.partition("/")
    if not separator or not tail:
        return "", value
    return provider_id_for(head), tail


# ===== Resolution =====

def resolve_destination(value: str) -> Tuple[str, str, str]:
    """Split ``kind[/option]`` into ``(destination, option, problem)``.

    An unknown option is reported rather than silently corrected: a note that
    says ``@/to/research/exhaustive`` was written by someone with a specific
    expectation, and quietly running a "standard" depth would meet none of it.
    """
    head, _, tail = (value or "").strip().strip("/").partition("/")
    head = head.lower()
    if head not in DESTINATIONS:
        known = ", ".join(DESTINATIONS)
        return "", "", f"unknown destination '{head or value}' — try one of: {known}"

    spec = DESTINATIONS[head]
    if not tail:
        return head, spec["default"], ""
    tail = tail.lower()
    if spec["options"] and tail not in spec["options"]:
        return head, spec["default"], (
            f"'{tail}' is not a {spec['option_label']} for {head} — "
            f"using '{spec['default']}' (options: {', '.join(spec['options'])})"
        )
    return head, tail, ""


def resolve(text: str, task: str = router_mod.TASK_CHAT) -> ResolvedDoc:
    """Turn a document (or a selection from one) into a ready-to-send turn."""
    resolved = ResolvedDoc(prompt=(text or "").strip())
    references = parse_references(text)
    resolved.references = references

    model_refs = [r for r in references if r.kind == "model"]
    file_refs = [r for r in references if r.kind == "file"]
    to_refs = [r for r in references if r.kind == "to"]

    # --- destination ---
    if len(to_refs) > 1:
        resolved.warnings.append(
            f"{len(to_refs)} @/to references — using the last one, '{to_refs[-1].value}'"
        )
        for other in to_refs[:-1]:
            other.detail = "overridden by a later @/to"
    if to_refs:
        chosen = to_refs[-1]
        destination, option, problem = resolve_destination(chosen.value)
        if not destination:
            chosen.detail = problem
            resolved.warnings.append(f"@/to/{chosen.value} — {problem}")
        else:
            resolved.destination, resolved.option = destination, option
            chosen.ok = not problem
            spec = DESTINATIONS[destination]
            chosen.detail = problem or (
                f"{spec['label']} · {option}" if option else spec["label"]
            )
            if problem:
                resolved.warnings.append(f"@/to/{chosen.value} — {problem}")

    # --- model ---
    if len(model_refs) > 1:
        resolved.warnings.append(
            f"{len(model_refs)} @/model references — using the last one, '{model_refs[-1].value}'"
        )
    if model_refs:
        chosen = model_refs[-1]
        provider_id, model = resolve_model_reference(chosen.value)
        head = chosen.value.partition("/")[0]
        if not provider_id and chosen.value.endswith("/") and providers_mod.get_provider(
                provider_id_for(head)):
            # Half-typed: the provider is named but the model is not. Say what
            # is missing rather than accusing them of skipping the provider.
            chosen.detail = f"pick a model for {head}"
            resolved.warnings.append(f"@/model/{chosen.value} — {chosen.detail}")
        elif not provider_id:
            chosen.detail = "name a provider, as in @/model/openai/…"
            resolved.warnings.append(f"@/model/{chosen.value} — {chosen.detail}")
        elif not providers_mod.get_provider(provider_id):
            chosen.detail = f"no provider called '{provider_id}'"
            resolved.warnings.append(f"@/model/{chosen.value} — {chosen.detail}")
        elif not providers_mod.usable(provider_id):
            chosen.detail = f"{provider_id} is not configured"
            resolved.warnings.append(
                f"@/model/{chosen.value} — {provider_id} is not configured, "
                "falling back to the usual route"
            )
        else:
            # A note bound for Research is routed as a research task, so an
            # unpinned note still lands on whatever model the user assigned to
            # research rather than on their chat model.
            resolved.route = router_mod.route(
                task=DESTINATION_TASKS.get(resolved.destination, task),
                model=model, provider=provider_id,
            )
            chosen.ok = True
            chosen.detail = f"{resolved.route.provider} · {resolved.route.model}"
        for other in model_refs[:-1]:
            other.detail = "overridden by a later @/model"

    # --- files ---
    blocks: List[str] = []
    total = 0
    seen: Dict[str, Reference] = {}
    for reference in file_refs:
        if reference.value in seen:
            first = seen[reference.value]
            reference.ok, reference.detail, reference.path = first.ok, "already cited", first.path
            continue
        seen[reference.value] = reference

        if len([r for r in file_refs if r.ok]) >= MAX_FILES:
            reference.detail = f"skipped — at the {MAX_FILES} file limit"
            resolved.warnings.append(f"@/file/{reference.value} — {reference.detail}")
            continue
        try:
            path = citable_path(reference.value)
            body = read_citation(path)
        except Exception as exc:
            reference.detail = str(exc)
            resolved.warnings.append(f"@/file/{reference.value} — {reference.detail}")
            continue

        if total + len(body) > MAX_TOTAL_CHARS:
            reference.detail = "skipped — the cited files exceed the total size budget"
            resolved.warnings.append(f"@/file/{reference.value} — {reference.detail}")
            continue

        total += len(body)
        reference.ok = True
        reference.path = path
        reference.chars = len(body)
        reference.detail = f"{len(body)} characters"
        blocks.append(f"===== {reference.value} ({path}) =====\n{body}")

    if blocks:
        resolved.context = (
            "The user's document cites these files. Their contents at send time:\n\n"
            + "\n\n".join(blocks)
        )
    return resolved


# ===== Candidates for the '@' menu =====

def _cached(key: str, build):
    """Cache by key, but only within one version of the provider registry.

    The TTL keeps the menu snappy; the version keeps it honest. Pasting a key
    should show that provider's models on the next keystroke, not two minutes
    later, so any registry change retires every entry cached before it.
    """
    key = f"{providers_mod.registry_version()}:{key}"
    hit = _candidate_cache.get(key)
    if hit and time.time() - hit[0] < CANDIDATE_TTL_SECONDS:
        return hit[1]
    value = build()
    _candidate_cache[key] = (time.time(), value)
    return value


def clear_candidate_cache():
    _candidate_cache.clear()


def file_candidates(query: str = "", limit: int = 40) -> List[Dict[str, str]]:
    """Citable files: the agent workspace, plus everything already indexed.

    Workspace files are offered as relative paths because that is how a plan
    about your own code reads; indexed documents keep their absolute path.
    """
    query = (query or "").lower()
    results: List[Dict[str, str]] = []
    seen = set()

    def offer(value: str, label: str, source: str):
        if value in seen:
            return
        if query and query not in value.lower() and query not in label.lower():
            return
        seen.add(value)
        results.append({"value": value, "label": label, "source": source})

    workspace = agent_tools.workspace_root()
    for root, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            relative = os.path.relpath(os.path.join(root, name), workspace)
            offer(relative, name, "workspace")
            if len(results) >= limit:
                return results

    for document in indexer.list_documents(limit=500):
        path = document.get("path") or ""
        if path:
            offer(path, os.path.basename(path), "indexed")
            if len(results) >= limit:
                break
    return results


def destination_candidates(query: str = "") -> List[Dict[str, str]]:
    """Where a note can be sent, offered before the option.

    Both the bare destination and each of its options are listed, so a note can
    be finished in one pick (``@/to/research``) or two (``@/to/research/deep``)
    without the menu insisting on the longer path.
    """
    query = (query or "").lower()
    offered: List[Dict[str, str]] = []
    for name, spec in DESTINATIONS.items():
        if not query or name.startswith(query):
            offered.append({"value": name, "label": spec["label"], "source": spec["help"]})
        for option in spec["options"]:
            value = f"{name}/{option}"
            if query and not value.startswith(query):
                continue
            offered.append({
                "value": value,
                "label": f"{spec['label']} — {option}",
                "source": f"{spec['option_label']}: {option}",
            })
    return offered


def provider_candidates(query: str = "") -> List[Dict[str, str]]:
    """Providers a note may name, offered before the model list.

    Only providers that are enabled *and* have whatever they need to run are
    listed — picking one whose key is missing would only produce a warning two
    steps later.
    """
    query = (query or "").lower()
    offered: List[Dict[str, str]] = []
    for provider in providers_mod.list_providers(include_disabled=False):
        if not provider["configured"]:
            continue
        name = "local" if provider["id"] == providers_mod.LOCAL_PROVIDER else provider["id"]
        if query and not name.startswith(query):
            continue
        offered.append({
            "value": name,
            "label": name,
            "source": provider["label"],
        })
    return offered


def model_candidates(provider_id: str, query: str = "", limit: int = 200) -> List[Dict[str, str]]:
    """Every model one provider can serve with the key on file.

    The list comes from the provider's own endpoint rather than anything
    hardcoded, so it reflects what this key is actually entitled to.
    """
    provider_id = provider_id_for(provider_id)
    provider = providers_mod.get_provider(provider_id)
    if not provider:
        return []

    def build():
        try:
            return providers_mod.list_models(provider_id).get("models", [])
        except Exception:
            return []

    models = _cached(f"models:{provider_id}", build)
    query = (query or "").lower()
    name = "local" if provider_id == providers_mod.LOCAL_PROVIDER else provider_id
    return [
        {"value": f"{name}/{model}", "label": model, "source": provider["label"]}
        for model in models
        if not query or query in model.lower()
    ][:limit]
