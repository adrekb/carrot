"""Task-aware model routing.

Local-first should not mean local-only, and it should not mean one-cloud-only
either. Every model call in Carrot names the *task* it is performing, and the
router maps that task to a provider and a model.

Three things are configurable, and they compose:

* **Providers** (``providers.py``) — Ollama plus any number of BYOK endpoints
  speaking the Anthropic or OpenAI wire format.
* **Tasks** — the seven built-ins (chat, code, reasoning, classify, summarize,
  extract, recap) plus any the user defines. A custom task is a first-class
  routing target: name it "checking", point it at a provider and model, and
  call it with ``?task=checking``.
* **Assignments** — an explicit ``task -> (provider, model)`` mapping. This is
  the direct answer to "model A for recap, model B for checking": an assignment
  always wins over the automatic escalation rules below.

With no assignment, a task runs on-device unless the user opted it into
escalation, and the high-volume tasks never escalate at all.

Every provider path emits the same event shape as
``OllamaClient.chat_stream_events`` (``{'type': 'thinking'|'content'|'tool_calls'}``)
so the agentic chat loop is provider-agnostic and only written once.
"""

from __future__ import annotations

import json
import logging
import re

LOG = logging.getLogger(__name__)
from dataclasses import dataclass, asdict
from typing import Any, Dict, Generator, List, Optional

from . import providers as providers_mod
from .config import get_config, set_config

PROVIDER_LOCAL = providers_mod.LOCAL_PROVIDER  # "ollama"
PROVIDER_CLOUD = "anthropic"

# Tasks the rest of Carrot routes by.
TASK_CHAT = "chat"
TASK_CODE = "code"
TASK_REASONING = "reasoning"
TASK_CLASSIFY = "classify"
TASK_SUMMARIZE = "summarize"
TASK_EXTRACT = "extract"
TASK_RECAP = "recap"
TASK_RESEARCH = "research"
TASK_AGENT = "agent"

TASKS = (
    TASK_CHAT, TASK_CODE, TASK_REASONING, TASK_CLASSIFY,
    TASK_SUMMARIZE, TASK_EXTRACT, TASK_RECAP, TASK_RESEARCH, TASK_AGENT,
)

# Cheap, high-volume tasks that should never escalate *automatically* — they run
# on every message and would make cloud routing expensive and slow for no
# quality gain. An explicit assignment still overrides this; it is a default,
# not a prohibition.
LOCAL_ONLY_TASKS = frozenset({TASK_CLASSIFY, TASK_EXTRACT, TASK_SUMMARIZE})

BUILTIN_TASK_SPECS: Dict[str, Dict[str, Any]] = {
    TASK_CHAT: {"label": "Chat", "description": "Ordinary conversation turns."},
    TASK_CODE: {"label": "Code", "description": "Writing and editing code."},
    TASK_REASONING: {"label": "Reasoning", "description": "Hard multi-step problems."},
    TASK_CLASSIFY: {"label": "Classify", "description": "Query classification, run on every search."},
    TASK_SUMMARIZE: {"label": "Summarize", "description": "Rolling conversation summaries."},
    TASK_EXTRACT: {"label": "Extract", "description": "Pulling durable facts out of a turn."},
    TASK_RECAP: {"label": "Recap", "description": "The morning briefing."},
    TASK_RESEARCH: {"label": "Research", "description": "Carrot Research: planning, reading sources, and writing cited reports."},
    TASK_AGENT: {"label": "Agent", "description": "Carrot Agent: deciding the next action while driving the browser or the desktop."},
}

DEFAULT_CLOUD_MODEL = "claude-opus-5"
DEFAULT_CLOUD_EFFORT = "high"

# Streaming gets a large ceiling (no HTTP timeout concern); non-streaming stays
# conservative so a slow response cannot outlive the request.
CLOUD_MAX_TOKENS_STREAM = 64000
CLOUD_MAX_TOKENS_SYNC = 16000

# Server-side refusal fallback: on a policy decline the API re-runs the request
# on Anthropic's recommended fallback model instead of returning the refusal.
CLOUD_BETAS = ["server-side-fallback-2026-07-01"]

# Local model suggestions by available RAM. Bigger weights need headroom beyond
# the file size for context, so the thresholds are deliberately generous.
HARDWARE_TIERS = [
    (64, "qwen3:32b", "64GB+ of RAM comfortably runs a 32B model"),
    (32, "qwen3:14b", "32GB of RAM fits a 14B model with room for context"),
    (16, "gemma3:12b", "16GB of RAM fits a 12B model"),
    (8, "llama3.2:3b", "8GB of RAM is best served by a 3B model"),
    (0, "llama3.2:1b", "under 8GB of RAM needs the smallest model"),
]

TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# Model-name families that identify their provider on sight. Longest-lived
# naming conventions only — a guess here is a fallback, never a substitute for
# the provider the caller actually named. First match wins.
MODEL_FAMILIES = (
    ("claude", PROVIDER_CLOUD),
    ("gpt-", "openai"),
    ("chatgpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4-", "openai"),
    ("codestral", "mistral"),
    ("devstral", "mistral"),
    ("magistral", "mistral"),
    ("ministral", "mistral"),
    ("mistral", "mistral"),
    ("pixtral", "mistral"),
    ("open-mistral", "mistral"),
    ("open-mixtral", "mistral"),
    ("deepseek", "deepseek"),
    ("grok", "xai"),
)


@dataclass
class Route:
    """The resolved destination for one model call."""

    task: str
    provider: str
    model: str
    reason: str
    effort: Optional[str] = None
    kind: str = providers_mod.KIND_OLLAMA
    local: bool = True
    # True when the task was read off the message rather than named by the
    # caller. The UI says so on the turn's route line: a model the user did not
    # choose has to explain itself.
    auto: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ===== Tasks =====

def custom_tasks() -> List[Dict[str, Any]]:
    """User-defined routing targets."""
    raw = get_config().get("custom_tasks", [])
    return [t for t in raw if isinstance(t, dict) and t.get("id") and t["id"] not in TASKS]


def tasks() -> List[Dict[str, Any]]:
    """Every routable task, built-ins first."""
    listed = [
        {
            "id": task,
            "label": BUILTIN_TASK_SPECS[task]["label"],
            "description": BUILTIN_TASK_SPECS[task]["description"],
            "local_only": task in LOCAL_ONLY_TASKS,
            "builtin": True,
        }
        for task in TASKS
    ]
    for spec in custom_tasks():
        listed.append({
            "id": spec["id"],
            "label": spec.get("label") or spec["id"],
            "description": spec.get("description", ""),
            "local_only": bool(spec.get("local_only", False)),
            "builtin": False,
        })
    return listed


def task_ids() -> List[str]:
    return [task["id"] for task in tasks()]


def is_local_only(task: str) -> bool:
    if task in TASKS:
        return task in LOCAL_ONLY_TASKS
    for spec in custom_tasks():
        if spec["id"] == task:
            return bool(spec.get("local_only", False))
    return False


def add_task(task_id: str, label: str = "", description: str = "", local_only: bool = False) -> Dict[str, Any]:
    """Define a custom task. Re-adding an existing one updates it."""
    task_id = (task_id or "").strip().lower()
    if not TASK_ID_PATTERN.match(task_id):
        raise ValueError("task id must be 1-40 characters of lowercase letters, digits, '-' or '_'")
    if task_id in TASKS:
        raise ValueError(f"'{task_id}' is a built-in task")

    existing = custom_tasks()
    spec = {
        "id": task_id,
        "label": label or task_id,
        "description": description,
        "local_only": bool(local_only),
    }
    for index, current in enumerate(existing):
        if current["id"] == task_id:
            existing[index] = spec
            break
    else:
        existing.append(spec)
    set_config("custom_tasks", existing)
    return {**spec, "builtin": False}


def delete_task(task_id: str) -> bool:
    """Remove a custom task and its assignment. Built-ins cannot be deleted."""
    if task_id in TASKS:
        raise ValueError(f"'{task_id}' is a built-in task and cannot be deleted")
    existing = custom_tasks()
    remaining = [t for t in existing if t["id"] != task_id]
    if len(remaining) == len(existing):
        return False
    set_config("custom_tasks", remaining)
    clear_route(task_id)
    return True


# ===== Assignments =====

def _infer_provider(model: str) -> str:
    """Best guess at the provider for a bare model name.

    Only used when the caller did not name one — an explicit ``model=`` from the
    UI's model picker, or a route stored by an older build as a plain string.

    Guessing "on-device" for everything that is not a Claude model was wrong in
    the way that hurts most: picking ``mistral-medium`` sent the turn to Ollama,
    which does not have it, and the UI cheerfully labelled the hosted model
    "(on-device)" while the call failed.

    An Ollama tag (``gemma4:e4b``, ``mistral:7b``) is local by construction, so
    that is checked first — a locally pulled Mistral must not be mistaken for
    the hosted one. Otherwise a known model family names its vendor, but only
    when that provider is actually configured: guessing "mistral" for someone
    who never added a Mistral key would trade a wrong label for a dead end.
    """
    name = (model or "").strip().lower()
    if not name or ":" in name:
        return PROVIDER_LOCAL
    for prefix, provider_id in MODEL_FAMILIES:
        if name.startswith(prefix):
            if provider_id == PROVIDER_CLOUD or providers_mod.usable(provider_id):
                return provider_id
            break
    # "vendor/model" is OpenRouter's naming, and nobody else's.
    if "/" in name and providers_mod.usable("openrouter"):
        return "openrouter"
    return PROVIDER_LOCAL


def _normalize_assignment(value: Any) -> Optional[Dict[str, Any]]:
    """Coerce a stored assignment into ``{provider, model, effort}``.

    Older builds stored a bare model string, so that shape has to keep working.
    """
    if isinstance(value, str) and value:
        return {"provider": _infer_provider(value), "model": value, "effort": None}
    if isinstance(value, dict) and value.get("model"):
        return {
            "provider": value.get("provider") or _infer_provider(value["model"]),
            "model": value["model"],
            "effort": value.get("effort"),
        }
    return None


def assignments() -> Dict[str, Dict[str, Any]]:
    """Every explicit task assignment, normalized."""
    raw = get_config().get("model_routes", {})
    if not isinstance(raw, dict):
        return {}
    resolved = {}
    for task, value in raw.items():
        normalized = _normalize_assignment(value)
        if normalized:
            resolved[task] = normalized
    return resolved


def assignment(task: str) -> Optional[Dict[str, Any]]:
    return assignments().get(task)


def set_route(
    task: str,
    model: str,
    provider: Optional[str] = None,
    effort: Optional[str] = None,
) -> Dict[str, Any]:
    """Pin a task to a provider and model.

    ``provider`` is optional so the older single-argument form still works; it
    is inferred from the model name when omitted.
    """
    if task not in task_ids():
        raise ValueError(f"unknown task: {task}")
    if not model:
        raise ValueError("a model is required")
    provider = provider or _infer_provider(model)
    providers_mod.require_provider(provider)

    routes = dict(get_config().get("model_routes", {}) or {})
    routes[task] = {"provider": provider, "model": model, "effort": effort}
    set_config("model_routes", routes)
    return routes


def clear_route(task: str) -> Dict[str, Any]:
    """Drop a task's assignment so it falls back to the automatic rules."""
    routes = dict(get_config().get("model_routes", {}) or {})
    routes.pop(task, None)
    set_config("model_routes", routes)
    return routes


# ===== Legacy cloud escalation =====

def cloud_api_key() -> str:
    """The Anthropic key, from config or the environment."""
    return providers_mod.api_key(PROVIDER_CLOUD)


def cloud_enabled() -> bool:
    config = get_config()
    return bool(config.get("cloud_enabled", False)) and bool(cloud_api_key())


def escalation_provider() -> str:
    """The provider automatic escalation targets. Anthropic unless changed."""
    return get_config().get("escalation_provider", PROVIDER_CLOUD) or PROVIDER_CLOUD


def escalation_model() -> str:
    config = get_config()
    if escalation_provider() == PROVIDER_CLOUD:
        return config.get("cloud_model", DEFAULT_CLOUD_MODEL)
    return config.get("escalation_model", "") or config.get("cloud_model", DEFAULT_CLOUD_MODEL)


def cloud_tasks() -> List[str]:
    """Tasks the user has opted into escalating."""
    configured = get_config().get("cloud_tasks", [TASK_REASONING, TASK_CODE])
    known = set(task_ids())
    return [t for t in configured if t in known and not is_local_only(t)]


def local_model(task: str) -> str:
    """The local model for a task, falling back to the configured default."""
    config = get_config()
    pinned = assignment(task)
    if pinned and pinned["provider"] == PROVIDER_LOCAL:
        return pinned["model"]
    return config.get("ollama_model", "gemma4:e4b")


# ===== Hardware-aware auto-pick =====

def recommend_local_model() -> Dict[str, Any]:
    """Suggest a local model that fits this machine's memory."""
    try:
        from .leaderboard import get_hardware_profile

        profile = get_hardware_profile()
        ram_gb = float(profile.get("ram_gb") or 0)
    except Exception:
        return {"model": None, "reason": "hardware profile unavailable", "ram_gb": 0}

    for threshold, model, reason in HARDWARE_TIERS:
        if ram_gb >= threshold:
            return {"model": model, "reason": reason, "ram_gb": ram_gb}
    return {"model": HARDWARE_TIERS[-1][1], "reason": HARDWARE_TIERS[-1][2], "ram_gb": ram_gb}


# ===== Routing =====

def _build(task: str, provider_id: str, model: str, reason: str, effort: Optional[str] = None) -> Route:
    provider = providers_mod.get_provider(provider_id)
    if provider:
        kind = provider["kind"]
    else:
        # An unregistered provider is not Ollama just because it is unknown.
        # Defaulting to the local kind is what put "(on-device)" next to a
        # hosted model — only the local provider may claim to be local.
        kind = (
            providers_mod.KIND_OLLAMA
            if provider_id == PROVIDER_LOCAL
            else providers_mod.KIND_OPENAI
        )
    return Route(
        task=task,
        provider=provider_id,
        model=model,
        reason=reason,
        effort=effort,
        kind=kind,
        local=kind == providers_mod.KIND_OLLAMA,
    )


def _local_route(task: str, reason: str) -> Route:
    return _build(task, PROVIDER_LOCAL, local_model(task), reason)


def route(
    task: str = TASK_CHAT,
    model: Optional[str] = None,
    prefer_cloud: bool = False,
    provider: Optional[str] = None,
) -> Route:
    """Resolve a task to a provider and model.

    Precedence, highest first:

    1. An explicit ``model`` — it is what the user picked in the UI.
    2. The task's assignment, if the user pinned one and its provider is usable.
    3. Automatic escalation, when the cloud is configured, the user opted this
       task in (or asked for it on this call), and the task is not high-volume.
    4. On-device.
    """
    if task not in task_ids():
        task = TASK_CHAT

    if model:
        return _build(task, provider or _infer_provider(model), model, "explicitly selected")

    pinned = assignment(task)
    if pinned:
        if providers_mod.usable(pinned["provider"]):
            return _build(
                task, pinned["provider"], pinned["model"],
                f"'{task}' is assigned to {pinned['provider']}", pinned.get("effort"),
            )
        if pinned["provider"] != PROVIDER_LOCAL:
            return _local_route(
                task, f"{pinned['provider']} is not configured — staying on-device"
            )

    if not is_local_only(task) and (prefer_cloud or task in cloud_tasks()):
        target = escalation_provider()
        if cloud_enabled() and providers_mod.usable(target):
            return _build(
                task, target, escalation_model(),
                f"'{task}' is routed to {target}",
                get_config().get("cloud_effort", DEFAULT_CLOUD_EFFORT),
            )
        if prefer_cloud:
            return _local_route(task, "cloud requested but not configured — staying local")

    return _local_route(task, f"'{task}' runs on-device")


# ===== Auto: reading the task off the message =====
#
# Routing has always been per-task, and the task was always named by the
# caller — so a chat turn took whatever the model picker said and nothing ever
# looked at what was actually asked. Auto closes that gap and nothing else: it
# names the task, then hands the decision to ``route`` above, so the same
# assignments, the same escalation rules and the same honest labelling apply.
# It is not a second routing layer.
#
# Patterns, not a model call. A classifier turn in front of every message would
# spend a round trip deciding something a handful of patterns get right, and
# would be one more thing to be slow or unavailable when Ollama is down —
# ``classify`` is local-only for exactly that reason. Every pattern carries the
# reason it fired, and the reason travels on the Route, so the trace can say
# why this turn went where it did instead of silently changing model.

# Unmistakably about code: the message is carrying the artifact, not describing
# the wish. Checked before the reasoning patterns because "why does this
# traceback happen" is a code question first.
AUTO_CODE_SIGNALS = (
    (r"```", "the message contains a code block"),
    (r"Traceback \(most recent call last\)", "the message contains a Python traceback"),
    (r"(?m)^\s*(diff --git|@@ -\d|[+-]{3} [ab]/)", "the message contains a diff"),
    (r"\b[\w./-]+\.(py|pyi|js|mjs|ts|tsx|jsx|rs|go|java|kt|rb|php|c|cc|cpp|h|hpp|cs|swift"
     r"|sh|ps1|sql|css|scss|html|json|ya?ml|toml|ini)\b", "the message names a source file"),
    (r"\b(segfault|segmentation fault|syntax error|compil(e|er|ation) error|type ?error"
     r"|null ?pointer|stack ?trace|merge conflict)\b", "the message names a build or runtime error"),
    (r"\b(git|npm|npx|pnpm|yarn|pip|poetry|uv|cargo|docker|kubectl|pytest|tsc|webpack|gradle|maven)\b",
     "the message names a developer tool"),
)

# Worth a stronger model even with no code in sight.
AUTO_REASONING_SIGNALS = (
    (r"\b(step by step|think (it )?through|reason through|work through|first principles)\b",
     "the message asks to be reasoned through"),
    (r"\b(prove|proof|derive|derivation|theorem|lemma)\b",
     "the message asks for a proof or a derivation"),
    (r"\b(trade-?offs?|pros and cons|compare|comparison|versus|vs\.?)\b",
     "the message asks for a comparison"),
    (r"\b(design|architect(ure)?|strategy|approach for|plan for)\b",
     "the message asks for a design or a plan"),
    (r"\b(analy[sz]e|analysis|evaluate|assess|critique|implications)\b",
     "the message asks for analysis"),
    (r"\bwhy (does|do|is|are|would|did|should|can'?t|cannot)\b",
     "the message asks why something is the case"),
)

# Code, but stated as an intention rather than shown. Checked after reasoning
# so "how would I restructure this" is read as the question it is.
AUTO_WEAK_CODE_SIGNALS = (
    (r"\b(refactor|debug|unit ?tests?|regexe?s?|stack ?overflow|codebase|repo(sitory)?)\b",
     "the message is about working on code"),
    (r"\b(write|fix|implement|rewrite|port|optimi[sz]e|profile)\b[^.\n]{0,48}"
     r"\b(function|class|method|script|regex|query|endpoint|component|module|test|code|bug)\b",
     "the message asks for code to be written or fixed"),
)

# A long message with a question in it is usually a real problem pasted in
# whole. Deliberately generous: below this, length says nothing.
AUTO_LONG_MESSAGE_CHARS = 700


def _compile(task: str, signals) -> List[Any]:
    return [(task, re.compile(pattern, re.IGNORECASE), why) for pattern, why in signals]


# Order is the whole design: strongest code evidence, then reasoning, then
# code stated as an intention. First match wins.
_AUTO_RULES = (
    _compile(TASK_CODE, AUTO_CODE_SIGNALS)
    + _compile(TASK_REASONING, AUTO_REASONING_SIGNALS)
    + _compile(TASK_CODE, AUTO_WEAK_CODE_SIGNALS)
)


def classify_message(message: str, coder: bool = False) -> Dict[str, str]:
    """Name the task a message is asking for, and why.

    Returns ``{'task', 'reason'}``. Only ever names a task that routes: chat,
    code or reasoning. It does not pick classify/extract/summarize — those are
    Carrot's own background work, never a thing the user asked for.
    """
    text = (message or "").strip()
    if coder:
        return {"task": TASK_CODE, "reason": "the Code tab is driving this turn"}
    if not text:
        return {"task": TASK_CHAT, "reason": "nothing to read"}

    for task, pattern, why in _AUTO_RULES:
        if pattern.search(text):
            return {"task": task, "reason": why}

    if len(text) >= AUTO_LONG_MESSAGE_CHARS and "?" in text:
        return {"task": TASK_REASONING, "reason": "the message is long and asks something"}

    return {"task": TASK_CHAT, "reason": "it reads as ordinary conversation"}


def auto_route(message: str, coder: bool = False, prefer_cloud: bool = False) -> Route:
    """Route a turn by what the message asks for rather than by a picked model."""
    verdict = classify_message(message, coder=coder)
    resolved = route(task=verdict["task"], prefer_cloud=prefer_cloud)
    resolved.auto = True
    resolved.reason = (
        f"auto: {verdict['reason']}, so it ran as '{verdict['task']}' — {resolved.reason}"
    )
    return resolved


def auto_enabled() -> bool:
    """Whether the model picker is set to Auto."""
    return bool(get_config().get("auto_model", False))


def set_auto(enabled: bool) -> bool:
    """Turn Auto on or off. Task assignments are left alone — they are what
    Auto routes *through*, so turning it off returns to exactly what was there.
    """
    set_config("auto_model", bool(enabled))
    return bool(enabled)


def auto_is_local() -> bool:
    """Whether every task Auto can pick currently runs on-device.

    The empty state promises "everything runs on your machine", and under Auto
    that promise is only true if none of the three reachable tasks escalates.
    """
    return all(route(task).local for task in (TASK_CHAT, TASK_CODE, TASK_REASONING))


def status() -> Dict[str, Any]:
    config = get_config()
    return {
        "cloud_enabled": cloud_enabled(),
        "cloud_configured": bool(cloud_api_key()),
        "cloud_model": config.get("cloud_model", DEFAULT_CLOUD_MODEL),
        "cloud_effort": config.get("cloud_effort", DEFAULT_CLOUD_EFFORT),
        "cloud_tasks": cloud_tasks(),
        "escalation_provider": escalation_provider(),
        "auto": auto_enabled(),
        "auto_local": auto_is_local(),
        "sdk_installed": _sdk_available(),
        "providers": providers_mod.list_providers(),
        "presets": providers_mod.PRESETS,
        "tasks": tasks(),
        "assignments": assignments(),
        "routes": {task: route(task).as_dict() for task in task_ids()},
        "recommendation": recommend_local_model(),
    }


# ===== Anthropic client =====

def _sdk_available() -> bool:
    try:
        import anthropic  # noqa: F401

        return True
    except ImportError:
        return False


def _client(provider_id: str = PROVIDER_CLOUD):
    """Build an Anthropic client, or raise a message the UI can show verbatim."""
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "the anthropic package is not installed — run: pip install 'carrot[cloud]'"
        ) from exc
    # Either a developer key or a subscription token, depending on the mode
    # this provider is in. They go in different headers, so the scheme decides
    # which constructor argument is used rather than the value being guessed at.
    from . import dualauth

    scheme, secret = dualauth.credential(provider_id)
    if not secret:
        raise RuntimeError(f"no API key configured for {provider_id}")
    provider = providers_mod.get_provider(provider_id) or {}
    base_url = provider.get("base_url") or ""
    kwargs = {"auth_token": secret} if scheme == "bearer" else {"api_key": secret}
    if base_url and base_url != providers_mod.BUILTIN_PROVIDERS["anthropic"]["base_url"]:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)


def _openai_client(provider_id: str):
    from .openai_client import OpenAICompatibleClient

    from . import dualauth

    provider = providers_mod.require_provider(provider_id)
    # OpenAI's wire format carries both a key and a subscription token in the
    # same Bearer header, so unlike Anthropic there is nothing to branch on.
    _, secret = dualauth.credential(provider_id)
    if provider["requires_key"] and not secret:
        raise RuntimeError(f"no API key configured for {provider_id}")
    return OpenAICompatibleClient(base_url=provider["base_url"], api_key=secret)


# ===== Message/tool translation =====

def _split_system(messages: List[Dict[str, Any]]):
    """Anthropic takes the system prompt out of band; Ollama keeps it inline."""
    system_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
    rest = [m for m in messages if m.get("role") != "system"]
    return "\n\n".join(system_parts), rest


# ===== Paying once for the part that never changes =====
#
# Every round of an agentic turn re-sends the same prefix: the directive and
# the whole tool schema, which `prompt_overhead()` measures at a third of an
# 8k window on its own. That was being billed and re-processed on every call,
# and a turn now runs until the context fills rather than for eight rounds, so
# the same bytes were going over the wire dozens of times.
#
# Anthropic caches a prefix when it is marked, and reads it back at a fraction
# of the input price with a shorter time to first token. What is marked is the
# system prompt and the tools — the stable part. The conversation is
# deliberately not marked: it grows every round, so a marker on it would write
# a new cache entry each time and pay the write premium for a prefix nothing
# reuses.
#
# There is a floor because a cache *write* costs more than an ordinary call.
# Below roughly a thousand tokens there is nothing to amortise, and marking a
# two-line prompt makes it more expensive rather than less.
CACHE_MIN_TOKENS = 1024


def _worth_caching(system: str, tools: List[Dict[str, Any]]) -> bool:
    from . import context_windows as ctxwin

    try:
        import json as _json

        size = ctxwin.estimate_tokens(system or "")
        if tools:
            size += ctxwin.estimate_tokens(_json.dumps(tools))
        return size >= CACHE_MIN_TOKENS
    except Exception:
        return False


def _cached_system(system: str):
    """The system prompt as a cacheable block."""
    return [{"type": "text", "text": system,
             "cache_control": {"type": "ephemeral"}}]


def _cached_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tools with the breakpoint on the last one.

    One marker, not one per tool: the cache is a prefix, so marking the final
    definition caches every definition before it. Four markers is the limit
    per request and spending them on individual tools would cache the same
    bytes four times.
    """
    if not tools:
        return tools
    marked = list(tools)
    marked[-1] = {**marked[-1], "cache_control": {"type": "ephemeral"}}
    return marked


def _to_anthropic_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Ollama-shaped history into Anthropic content blocks.

    Ollama models tool results as a ``tool`` role message; Anthropic models them
    as a ``tool_result`` block inside a user turn keyed by ``tool_use_id``.
    Consecutive tool results are merged into one user turn, which is what the
    API expects when several tools ran in parallel.
    """
    converted: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""

        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id") or message.get("name") or "unknown",
                "content": str(content)[:20000],
            }
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue

        if role == "assistant" and message.get("tool_calls"):
            blocks: List[Dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for call in message["tool_calls"]:
                function = call.get("function", {})
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or f"call_{len(blocks)}",
                        "name": function.get("name", ""),
                        "input": arguments,
                    }
                )
            converted.append({"role": "assistant", "content": blocks})
            continue

        if not content:
            continue
        converted.append({"role": role if role in ("user", "assistant") else "user", "content": content})

    return converted


def _to_anthropic_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Convert Ollama function-calling schemas into Anthropic tool definitions."""
    if not tools:
        return []
    converted = []
    for tool in tools:
        function = tool.get("function", tool)
        name = function.get("name")
        if not name:
            continue
        converted.append(
            {
                "name": name,
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def _tool_calls_from_message(message) -> List[Dict[str, Any]]:
    """Extract tool_use blocks as Ollama-shaped calls, preserving the block id.

    The id round-trips back as ``tool_call_id`` on the tool result, which is how
    Anthropic pairs a result with its call.
    """
    calls = []
    for block in message.content:
        if getattr(block, "type", None) == "tool_use":
            calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": block.input or {}},
                }
            )
    return calls


REFUSAL_MESSAGE = (
    "The model declined this request. Rephrasing it, or switching this task back "
    "to a local model in Settings, may work."
)


# ===== Unified calls =====

def stream_events(
    resolved: Route,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    on_wait=None,
) -> Generator[Dict[str, Any], None, None]:
    """Stream one turn, retrying a failure that happened before it started.

    ``with_rate_limit_retry`` covered the non-streaming calls only, which left
    the two paths people actually use — chat and the Code tab — with no retry
    at all. A rate limit or a dropped connection ended the turn, and on a
    coding turn that had already read four files and run a command, it ended
    it with the work done and nothing written.

    **Only before the first event.** Once tokens are out they are on the
    user's screen and in ``content_parts``; running the request again would
    replay the answer from the top and append it to the half already shown.
    So a failure mid-stream is still a failure — it is reported, not retried
    — and this only covers the case where the provider refused to start,
    which is what a 429 and a connection timeout both are.

    The pacer is told about a rate limit as well as this call sleeping on it,
    because four subagents streaming at once will otherwise each discover the
    limit by walking into it.
    """
    import random
    import time as _time

    from . import pacing

    provider = getattr(resolved, "provider", "") or ""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        started = False
        try:
            for event in _stream_once(resolved, messages, tools):
                started = True
                yield event
            return
        except Exception as exc:
            limited = _is_rate_limited(exc)
            if started or attempt >= RATE_LIMIT_RETRIES or not (limited or _is_transient(exc)):
                raise
            delay = _retry_after_seconds(exc)
            if limited and provider:
                try:
                    pacing.for_provider(provider).on_rate_limited(delay)
                except Exception:
                    pass
            if delay is None:
                delay = min(RATE_LIMIT_BASE_DELAY * (2 ** attempt), RATE_LIMIT_MAX_DELAY)
                delay += random.uniform(0, delay * 0.25)
            reason = "rate limited" if limited else "provider busy"
            if on_wait:
                on_wait(attempt + 1, delay, reason)
            LOG.info("%s before the stream began; retrying in %.1fs (attempt %d/%d)",
                     reason, delay, attempt + 1, RATE_LIMIT_RETRIES)
            _time.sleep(delay)


def _stream_once(
    resolved: Route,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Generator[Dict[str, Any], None, None]:
    """One attempt at the stream, in whichever provider's shape.

    Yields the same typed events as ``OllamaClient.chat_stream_events`` so the
    agentic loop does not need to know which provider ran.
    """
    kind = _kind_of(resolved)

    if kind == providers_mod.KIND_OLLAMA:
        from .ollama_client import OllamaClient

        yield from OllamaClient().chat_stream_events(messages, model=resolved.model, tools=tools)
        return

    if kind == providers_mod.KIND_OPENAI:
        yield from _openai_client(resolved.provider).chat_stream_events(
            messages, model=resolved.model, tools=tools
        )
        return

    client = _client(resolved.provider)
    system, conversation = _split_system(messages)
    request: Dict[str, Any] = {
        "model": resolved.model,
        "max_tokens": CLOUD_MAX_TOKENS_STREAM,
        "messages": _to_anthropic_messages(conversation),
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": resolved.effort or DEFAULT_CLOUD_EFFORT},
        "betas": CLOUD_BETAS,
        "fallbacks": "default",
    }
    anthropic_tools = _to_anthropic_tools(tools)
    # Marked together or not at all: the cache is a prefix, and a marker on
    # the system prompt only helps if the tools before it are stable too.
    cacheable = _worth_caching(system, anthropic_tools)
    if system:
        request["system"] = _cached_system(system) if cacheable else system
    if anthropic_tools:
        request["tools"] = _cached_tools(anthropic_tools) if cacheable else anthropic_tools

    with client.beta.messages.stream(**request) as stream:
        for event in stream:
            if event.type != "content_block_delta":
                continue
            delta = event.delta
            if delta.type == "thinking_delta":
                yield {"type": "thinking", "text": delta.thinking}
            elif delta.type == "text_delta":
                yield {"type": "content", "text": delta.text}
        final = stream.get_final_message()

    if final.stop_reason == "refusal":
        yield {"type": "content", "text": REFUSAL_MESSAGE}
        return

    calls = _tool_calls_from_message(final)
    if calls:
        yield {"type": "tool_calls", "calls": calls}


# ===== Rate limits =====
#
# Research fans out across parallel workers and makes many model calls per
# run — planning, extraction, verification, then the write. A hosted
# provider will start refusing (HTTP 429) well before the run is done, and
# losing the final write after minutes of gathering evidence is the worst
# possible moment to fail. Every cloud call therefore retries with
# exponential backoff, honouring Retry-After when the server sends it.

RATE_LIMIT_RETRIES = 5
RATE_LIMIT_BASE_DELAY = 2.0
RATE_LIMIT_MAX_DELAY = 60.0


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Seconds the provider asked us to wait, if it said so."""
    for attr in ("response", "http_response"):
        response = getattr(exc, attr, None)
        headers = getattr(response, "headers", None)
        if not headers:
            continue
        for key in ("retry-after", "Retry-After", "x-ratelimit-reset-after"):
            value = headers.get(key) if hasattr(headers, "get") else None
            if value:
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    pass
    return None


def _is_rate_limited(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limited" in text


def _is_transient(exc: Exception) -> bool:
    """Overloaded/unavailable is worth waiting out too."""
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None)
    if status in (500, 502, 503, 504, 529):
        return True
    text = str(exc).lower()
    return any(s in text for s in ("overloaded", "service unavailable", "bad gateway",
                                   "timed out", "timeout", "connection reset"))


def with_rate_limit_retry(call, *, on_wait=None, retries: int = RATE_LIMIT_RETRIES,
                          provider: str = ""):
    """Run ``call``, pacing it and retrying when the provider throttles us.

    Two mechanisms, doing different jobs. The pacer sets how fast requests may
    leave *before* one is sent, and learns the provider's real limit over the
    run. The retry loop handles a request that was refused anyway. Retrying
    alone means every worker discovers the limit by tripping it, over and over
    — the pacer is what stops the run from repeatedly running into the wall.

    ``on_wait(attempt, delay, reason)`` is invoked before each sleep so a
    long research run can show that it is waiting rather than hung.
    """
    import random
    import time

    from . import pacing

    pacer = pacing.for_provider(provider) if provider else None

    for attempt in range(retries + 1):
        if pacer:
            waited = pacer.wait()
            if waited > 0.5 and on_wait:
                on_wait(attempt, waited, "pacing to stay under the rate limit")
        try:
            result = call()
            if pacer:
                pacer.on_success()
            return result
        except Exception as exc:
            limited = _is_rate_limited(exc)
            if attempt >= retries or not (limited or _is_transient(exc)):
                raise
            delay = _retry_after_seconds(exc)
            if pacer and limited:
                # Slow every later request to this provider, not just this
                # retry — otherwise the other workers walk into the same wall.
                pacer.on_rate_limited(delay)
            if delay is None:
                delay = min(RATE_LIMIT_BASE_DELAY * (2 ** attempt), RATE_LIMIT_MAX_DELAY)
                delay += random.uniform(0, delay * 0.25)   # de-sync parallel workers
            reason = "rate limited" if limited else "provider busy"
            if on_wait:
                on_wait(attempt + 1, delay, reason)
            LOG.info("%s; retrying in %.1fs (attempt %d/%d)", reason, delay, attempt + 1, retries)
            time.sleep(delay)


def complete(resolved: Route, messages: List[Dict[str, Any]]) -> str:
    """Non-streaming completion through whichever provider the route names."""
    kind = _kind_of(resolved)

    if kind == providers_mod.KIND_OLLAMA:
        from .ollama_client import OllamaClient

        return OllamaClient().chat(messages, model=resolved.model)

    if kind == providers_mod.KIND_OPENAI:
        return with_rate_limit_retry(lambda: _openai_client(resolved.provider).chat(
            messages, model=resolved.model, max_tokens=CLOUD_MAX_TOKENS_SYNC
        ), provider=resolved.provider)

    client = _client(resolved.provider)
    system, conversation = _split_system(messages)
    request: Dict[str, Any] = {
        "model": resolved.model,
        "max_tokens": CLOUD_MAX_TOKENS_SYNC,
        "messages": _to_anthropic_messages(conversation),
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": resolved.effort or DEFAULT_CLOUD_EFFORT},
        "betas": CLOUD_BETAS,
        "fallbacks": "default",
    }
    if system:
        request["system"] = (_cached_system(system)
                             if _worth_caching(system, []) else system)

    response = with_rate_limit_retry(lambda: client.beta.messages.create(**request),
                                     provider=resolved.provider or "anthropic")
    if response.stop_reason == "refusal":
        return REFUSAL_MESSAGE
    return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")


# ===== Vision =====

MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}


def vision_complete(resolved: Route, prompt: str, image_path: str) -> str:
    """Ask a model about an image, whichever provider the route names.

    Each protocol carries images differently — Ollama takes bare base64 on the
    message, OpenAI takes a data URI content part, Anthropic takes a typed
    source block — so the encoding is done once here rather than in every caller.
    """
    import base64
    import os

    extension = os.path.splitext(image_path)[1].lower()
    if extension not in MEDIA_TYPES:
        raise ValueError(
            f"'{extension}' is not an image format that can be sent to a model "
            f"({', '.join(sorted(MEDIA_TYPES))})"
        )
    with open(image_path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    media_type = MEDIA_TYPES[extension]
    kind = _kind_of(resolved)

    if kind == providers_mod.KIND_OLLAMA:
        from .ollama_client import OllamaClient

        return OllamaClient().chat(
            [{"role": "user", "content": prompt, "images": [encoded]}],
            model=resolved.model,
        )

    if kind == providers_mod.KIND_OPENAI:
        from .openai_client import OpenAICompatibleClient

        provider = providers_mod.require_provider(resolved.provider)
        client = OpenAICompatibleClient(
            base_url=provider["base_url"], api_key=providers_mod.api_key(resolved.provider)
        )
        return client.chat_raw([{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{media_type};base64,{encoded}"}},
            ],
        }], model=resolved.model)

    client = _client(resolved.provider)
    response = client.messages.create(
        model=resolved.model,
        max_tokens=CLOUD_MAX_TOKENS_SYNC,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": media_type, "data": encoded}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )


def _kind_of(resolved: Route) -> str:
    """The wire protocol for a route, re-resolved in case the Route is stale."""
    if resolved.kind in providers_mod.KINDS:
        return resolved.kind
    provider = providers_mod.get_provider(resolved.provider)
    return provider["kind"] if provider else providers_mod.KIND_OLLAMA
