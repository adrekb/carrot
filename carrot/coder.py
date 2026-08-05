"""The coding agent — the parts of Cline, Continue and Goose worth having.

Carrot could already read, write and run files. What it could not do is the
thing that separates a chat window with file access from a coding agent: work
in a disciplined loop that a person can follow, interrupt, and undo. Each of
the three open-source agents solves one piece of that well, and none of them
solves all of it. This module takes the piece each does best.

**Plan then act** (Cline). A coding turn has two phases. In *plan* the agent
may only read — list, grep, open files, ask questions — and its output is a
proposal. Nothing on disk moves until a person says go. In *act* the write
tools unlock. The split is enforced by which tools are offered, not by asking
the model to behave, because a model that can write will eventually write.

**Search/replace edits** (Cline). Rewriting a whole file to change one line
burns tokens proportional to file size and loses everything the model did not
bother to reproduce. An edit is a set of exact-match blocks instead, applied
with a whitespace-tolerant fallback, and it fails loudly rather than guessing
when a block does not match.

**Checkpoints** (Cline). Before the agent acts, the workspace's text files are
snapshotted. Any checkpoint can be restored whole. The existing file journal
already reverses one write at a time; a checkpoint reverses a whole train of
thought, which is what you actually want when an agent goes wrong on step 9.

**Project rules** (all three, incompatibly). Cline reads `.clinerules`,
Continue reads its rules files, Goose reads `.goosehints`, and the industry has
mostly converged on `AGENTS.md`. Carrot reads all of them, so a repo that is
already set up for any of those tools is already set up for Carrot.

**Recipes** (Goose). A task worth doing twice is worth saving: a named prompt
with typed parameters, its mode, and the tools it is allowed. Goose calls these
recipes and they are the most portable idea in the project.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import get_config, set_config
from .database import get_db

# ===== Modes =====

MODE_PLAN = "plan"
MODE_ACT = "act"
MODES = (MODE_PLAN, MODE_ACT)

# Tools that change something. In plan mode none of these are offered at all.
WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "run_command", "create_file", "delete_file",
    "git_commit", "git_checkout", "restore_checkpoint",
})


def normalize_mode(value: Optional[str]) -> str:
    """Anything unrecognized means plan. The cautious default is the safe one."""
    mode = (value or "").strip().lower()
    return mode if mode in MODES else MODE_PLAN


def tools_for_mode(names, mode: str) -> List[str]:
    """Filter a tool list down to what this mode may call."""
    allowed = list(names)
    if normalize_mode(mode) == MODE_ACT:
        return allowed
    return [n for n in allowed if _bare(n) not in WRITE_TOOLS]


def _bare(name: str) -> str:
    """Strip the ``carrot__`` namespace the chat loop adds when offering tools."""
    return name.split("__", 1)[1] if "__" in name else name


MODE_PREAMBLE = {
    MODE_PLAN: (
        "You are in PLAN mode. You may read the workspace but you cannot change "
        "it — the write tools are not available to you and asking for them will "
        "not produce them. Investigate, then propose: say which files you would "
        "change, what the change is, and what could go wrong. Ask about anything "
        "genuinely ambiguous instead of guessing. The user switches you to ACT "
        "mode when the plan is right."
    ),
    MODE_ACT: (
        "You are in ACT mode. Carry out the agreed plan. Prefer edit_file with "
        "exact search/replace blocks over rewriting whole files. After a change "
        "that should be runnable, run it and read the output. If you discover "
        "the plan was wrong, stop and say so rather than improvising something "
        "the user did not agree to."
    ),
}


# ===== Project rules =====
#
# Order matters: the more Carrot-specific and the more standard files come
# first, so a repo carrying several of them reads sensibly top to bottom.
RULE_FILES = (
    "AGENTS.md",
    ".carrotrules",
    "CLAUDE.md",
    ".clinerules",
    ".continuerules",
    ".goosehints",
    ".cursorrules",
    ".github/copilot-instructions.md",
)
RULES_DIRS = (".clinerules", ".continue/rules", ".carrot/rules")
MAX_RULES_CHARS = 24000


def load_rules(root: str) -> str:
    """Every project rules file this repo carries, concatenated and labelled.

    A repo already configured for Cline, Continue, Goose, Cursor or Copilot
    needs no extra file to be configured for Carrot.
    """
    if not root or not os.path.isdir(root):
        return ""
    chunks: List[str] = []
    seen: set = set()

    def take(path: str, label: str) -> None:
        real = os.path.realpath(path)
        if real in seen or not os.path.isfile(real):
            return
        seen.add(real)
        try:
            with open(real, "r", encoding="utf-8", errors="replace") as handle:
                body = handle.read().strip()
        except OSError:
            return
        if body:
            chunks.append(f"--- {label} ---\n{body}")

    for name in RULE_FILES:
        take(os.path.join(root, name), name)
    # A rules *directory* is how Cline and Continue handle multiple rule sets.
    for folder in RULES_DIRS:
        full = os.path.join(root, folder)
        if not os.path.isdir(full):
            continue
        for name in sorted(os.listdir(full)):
            if name.endswith((".md", ".txt", ".mdc")):
                take(os.path.join(full, name), f"{folder}/{name}")

    if not chunks:
        return ""
    text = "\n\n".join(chunks)
    if len(text) > MAX_RULES_CHARS:
        text = text[:MAX_RULES_CHARS] + "\n\n[rules truncated]"
    return (
        "Project rules, from the repository's own instruction files. Follow "
        "them; they outrank your general habits.\n\n" + text
    )


# ===== Search/replace edits =====
#
# The block format is Cline's, which is also aider's and roughly everyone's:
# a search section, a divider, a replace section. It is parsed strictly — a
# malformed block is an error, never a partial application.

SEARCH_OPEN = re.compile(r"^<{5,9} SEARCH\s*$|^-{5,9} SEARCH\s*$", re.M)
DIVIDER = re.compile(r"^={5,9}\s*$", re.M)
REPLACE_CLOSE = re.compile(r"^>{5,9} REPLACE\s*$|^\+{5,9} REPLACE\s*$", re.M)


class EditError(ValueError):
    """A block was malformed, or matched zero or many times."""


def parse_edit_blocks(text: str) -> List[Tuple[str, str]]:
    """Pull ``(search, replace)`` pairs out of a diff payload.

    Accepts both the ``<<<<<<< SEARCH`` and ``------- SEARCH`` spellings, since
    models trained on different agents emit different ones and rejecting the
    other spelling is a pointless failure.
    """
    blocks: List[Tuple[str, str]] = []
    pos = 0
    while True:
        opened = SEARCH_OPEN.search(text, pos)
        if not opened:
            break
        divider = DIVIDER.search(text, opened.end())
        if not divider:
            raise EditError("a SEARCH block has no ======= divider")
        closed = REPLACE_CLOSE.search(text, divider.end())
        if not closed:
            raise EditError("a SEARCH block has no REPLACE terminator")
        search = _strip_edges(text[opened.end():divider.start()])
        replace = _strip_edges(text[divider.end():closed.start()])
        blocks.append((search, replace))
        pos = closed.end()
    if not blocks:
        raise EditError(
            "no search/replace blocks found. Use:\n"
            "------- SEARCH\n<exact existing text>\n=======\n<new text>\n+++++++ REPLACE"
        )
    return blocks


def _strip_edges(section: str) -> str:
    """Drop the single newline the delimiters contribute, and nothing else.

    Indentation inside a block is meaningful, so nothing is stripped from the
    line starts — that was the bug that made every edit to indented code fail.
    """
    if section.startswith("\n"):
        section = section[1:]
    if section.endswith("\n"):
        section = section[:-1]
    return section


def apply_edits(content: str, blocks: List[Tuple[str, str]]) -> str:
    """Apply every block in order, or raise and change nothing.

    Exact match first. Failing that, a match ignoring trailing whitespace and
    line-ending differences — the two ways a model's copy of a file drifts from
    what is on disk without the code being different. Anything looser than that
    would be guessing, and a coding agent that guesses at edits is worse than
    one that refuses.
    """
    result = content
    for index, (search, replace) in enumerate(blocks, start=1):
        if search == "":
            # An empty search is "append", which is well-defined and useful.
            result = result + ("" if result.endswith("\n") or not result else "\n") + replace
            continue
        count = result.count(search)
        if count == 1:
            result = result.replace(search, replace, 1)
            continue
        if count > 1:
            raise EditError(
                f"block {index} matches {count} places — include more surrounding "
                f"context so it identifies exactly one"
            )
        loosened = _loose_replace(result, search, replace)
        if loosened is None:
            raise EditError(
                f"block {index} does not match the file. Read the file again and "
                f"copy the exact text you want to replace."
            )
        result = loosened
    return result


def _normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))


def _loose_replace(content: str, search: str, replace: str) -> Optional[str]:
    """Match on normalized lines, but splice using the file's real text."""
    lines = content.replace("\r\n", "\n").split("\n")
    target = _normalize(search).split("\n")
    if not target:
        return None
    stripped = [line.rstrip() for line in lines]
    hits = [
        i for i in range(len(lines) - len(target) + 1)
        if stripped[i:i + len(target)] == target
    ]
    if len(hits) != 1:
        return None
    start = hits[0]
    spliced = lines[:start] + replace.split("\n") + lines[start + len(target):]
    return "\n".join(spliced)


# ===== Checkpoints =====

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".next", "target", ".tox", ".idea",
}
TEXT_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt", ".html",
    ".css", ".scss", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".rs",
    ".go", ".java", ".c", ".h", ".cpp", ".hpp", ".rb", ".php", ".sql", ".swift",
    ".kt", ".lua", ".r", ".pl", ".cs", ".vue", ".svelte", ".xml", ".env.example",
}
MAX_FILE_BYTES = 512 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_CHECKPOINTS = 50


def _is_text(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in TEXT_SUFFIXES


def snapshot(root: str) -> Dict[str, str]:
    """Every text file in the workspace, keyed by relative path.

    Binaries and build output are skipped: restoring them is not what anyone
    means by "undo what the agent did", and carrying them would make a
    checkpoint too big to keep fifty of.
    """
    files: Dict[str, str] = {}
    total = 0
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if not _is_text(name):
                continue
            full = os.path.join(base, name)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, "r", encoding="utf-8") as handle:
                    body = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
            total += len(body)
            if total > MAX_SNAPSHOT_BYTES:
                return files
            files[os.path.relpath(full, root).replace(os.sep, "/")] = body
    return files


def create_checkpoint(root: str, label: str = "", conversation_id: Optional[str] = None) -> Dict[str, Any]:
    """Snapshot the workspace so this point can be returned to."""
    files = snapshot(root)
    entry_id = str(uuid.uuid4())[:12]
    conn = get_db()
    conn.execute(
        """INSERT INTO coder_checkpoints
           (id, label, root, files, conversation_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (entry_id, label or "checkpoint", root, json.dumps(files),
         conversation_id, datetime.now(timezone.utc).isoformat()),
    )
    # Keep the table bounded; the oldest checkpoint is the least useful one.
    conn.execute(
        """DELETE FROM coder_checkpoints WHERE id NOT IN (
               SELECT id FROM coder_checkpoints ORDER BY created_at DESC LIMIT ?)""",
        (MAX_CHECKPOINTS,),
    )
    conn.commit()
    conn.close()
    return {"id": entry_id, "label": label or "checkpoint", "files": len(files)}


def list_checkpoints(limit: int = 30) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        """SELECT id, label, root, conversation_id, created_at,
                  length(files) AS size
           FROM coder_checkpoints ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def restore_checkpoint(checkpoint_id: str) -> Dict[str, Any]:
    """Put every snapshotted file back, and report what moved.

    Files the agent *created* after the checkpoint are deleted, because leaving
    them behind would make "restore" mean "mostly restore" — the failure mode
    that makes people stop trusting undo.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM coder_checkpoints WHERE id = ?", (checkpoint_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise KeyError(f"no such checkpoint: {checkpoint_id}")

    root = row["root"]
    files = json.loads(row["files"])
    restored, removed = [], []
    for rel, body in files.items():
        full = os.path.join(root, rel.replace("/", os.sep))
        try:
            current = None
            if os.path.isfile(full):
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    current = handle.read()
            if current == body:
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(body)
            restored.append(rel)
        except OSError:
            continue

    for rel in sorted(set(snapshot(root)) - set(files)):
        try:
            os.remove(os.path.join(root, rel.replace("/", os.sep)))
            removed.append(rel)
        except OSError:
            continue

    return {"id": checkpoint_id, "restored": restored, "removed": removed}


def delete_checkpoint(checkpoint_id: str) -> bool:
    conn = get_db()
    cursor = conn.execute("DELETE FROM coder_checkpoints WHERE id = ?", (checkpoint_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


# ===== Recipes =====
#
# Goose's best portable idea: a saved task, with parameters, a mode and a tool
# allowlist. Stored in config rather than the database so a recipe can be
# exported, checked into a repo, and shared.

RECIPE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def recipes() -> List[Dict[str, Any]]:
    raw = get_config().get("coder_recipes", [])
    return [r for r in raw if isinstance(r, dict) and r.get("id")]


def get_recipe(recipe_id: str) -> Optional[Dict[str, Any]]:
    for recipe in recipes():
        if recipe["id"] == recipe_id:
            return recipe
    return None


def save_recipe(
    recipe_id: str,
    title: str,
    prompt: str,
    description: str = "",
    parameters: Optional[List[Dict[str, Any]]] = None,
    mode: str = MODE_PLAN,
    tools: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if not RECIPE_ID.match(recipe_id or ""):
        raise ValueError("a recipe id is lowercase letters, digits, - and _")
    if not (prompt or "").strip():
        raise ValueError("a recipe needs a prompt")
    recipe = {
        "id": recipe_id,
        "title": title or recipe_id,
        "description": description,
        "prompt": prompt,
        "parameters": [p for p in (parameters or []) if isinstance(p, dict) and p.get("name")],
        "mode": normalize_mode(mode),
        "tools": list(tools or []),
    }
    kept = [r for r in recipes() if r["id"] != recipe_id]
    set_config("coder_recipes", kept + [recipe])
    return recipe


def delete_recipe(recipe_id: str) -> bool:
    kept = [r for r in recipes() if r["id"] != recipe_id]
    if len(kept) == len(recipes()):
        return False
    set_config("coder_recipes", kept)
    return True


PARAM_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def render_recipe(recipe_id: str, values: Optional[Dict[str, Any]] = None) -> str:
    """Substitute ``{{name}}`` placeholders, refusing to run half-filled.

    A recipe that silently ran with an unfilled parameter would send the model
    a prompt containing the literal text ``{{path}}``, which is worse than an
    error because it looks like it worked.
    """
    recipe = get_recipe(recipe_id)
    if not recipe:
        raise KeyError(f"no such recipe: {recipe_id}")
    supplied = dict(values or {})
    for param in recipe.get("parameters", []):
        name = param["name"]
        if name not in supplied and param.get("default") is not None:
            supplied[name] = param["default"]

    missing = sorted({
        name for name in PARAM_PATTERN.findall(recipe["prompt"])
        if str(supplied.get(name, "")).strip() == ""
    })
    if missing:
        raise ValueError("missing recipe parameters: " + ", ".join(missing))
    return PARAM_PATTERN.sub(lambda m: str(supplied[m.group(1)]), recipe["prompt"])
