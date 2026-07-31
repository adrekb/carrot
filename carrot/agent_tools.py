"""Built-in agent tools.

The chat loop could only call MCP servers, which meant Carrot could not touch
its own workspace without the user installing something first. These are native
tools exposed to the model alongside MCP tools, covering the plan -> edit -> run
-> verify cycle plus recall over Carrot's own stores.

Two safety properties hold for every tool here:

* **Mutating tools ask first.** Anything that writes a file, runs a command, or
  creates data raises an approval request that blocks until the user answers or
  the request times out. Read-only tools run unattended.
* **File writes are reversible.** Every write records the previous contents in
  ``file_journal`` before touching disk, so any edit can be reverted with its
  diff shown first.
"""

from __future__ import annotations

import difflib
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .config import get_config
from .database import get_db

TOOL_PREFIX = "carrot"
APPROVAL_TIMEOUT_SECONDS = 120
MAX_READ_CHARS = 20000
MAX_LIST_ENTRIES = 300
MAX_GREP_MATCHES = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===== Workspace resolution =====

def workspace_root() -> str:
    root = get_config().get("code_workspace_dir", "")
    if not root:
        root = os.path.join(os.path.expanduser("~"), "CarrotProjects")
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True)
    return root


def resolve(rel_path: str) -> str:
    """Resolve a workspace-relative path, refusing anything that escapes it."""
    root = workspace_root()
    full = os.path.abspath(os.path.join(root, rel_path or ""))
    if full != root and not full.startswith(root + os.sep):
        raise PermissionError(f"path escapes the workspace root: {rel_path}")
    return full


# ===== Approval gate =====

class ApprovalRequest:
    """A pending permission prompt the model is blocked on."""

    def __init__(self, tool: str, arguments: Dict[str, Any], summary: str, risk: str):
        self.id = str(uuid.uuid4())[:12]
        self.tool = tool
        self.arguments = arguments
        self.summary = summary
        self.risk = risk
        self.created_at = _now()
        self.decision: Optional[str] = None
        self.event = threading.Event()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "arguments": self.arguments,
            "summary": self.summary,
            "risk": self.risk,
            "created_at": self.created_at,
            "decision": self.decision,
        }


_pending: Dict[str, ApprovalRequest] = {}
_pending_lock = threading.Lock()
# Tools the user chose to stop being asked about, until the process restarts.
_session_allowed: set = set()


def pending_approvals() -> List[Dict[str, Any]]:
    with _pending_lock:
        return [req.as_dict() for req in _pending.values() if req.decision is None]


def resolve_approval(approval_id: str, decision: str, remember: bool = False) -> bool:
    """Answer a pending approval. ``decision`` is 'allow' or 'deny'."""
    with _pending_lock:
        request = _pending.get(approval_id)
    if request is None:
        return False
    request.decision = "allow" if decision == "allow" else "deny"
    if remember and request.decision == "allow":
        _session_allowed.add(request.tool)
    request.event.set()
    return True


def reset_session_approvals():
    _session_allowed.clear()


def _request_approval(tool: str, arguments: Dict[str, Any], summary: str, risk: str, emit: Optional[Callable]):
    """Block until the user answers, or auto-deny on timeout.

    ``emit`` pushes the prompt down the SSE stream; without it there is no UI
    listening, so the call is denied rather than left hanging.
    """
    if tool in _session_allowed:
        return True, "allowed for this session"
    if emit is None:
        return False, "approval required but no interactive channel is attached"

    request = ApprovalRequest(tool, arguments, summary, risk)
    with _pending_lock:
        _pending[request.id] = request
    emit({"approval_request": request.as_dict()})

    granted = request.event.wait(APPROVAL_TIMEOUT_SECONDS)
    with _pending_lock:
        _pending.pop(request.id, None)
    if not granted:
        emit({"approval_resolved": {"id": request.id, "decision": "timeout"}})
        return False, f"approval timed out after {APPROVAL_TIMEOUT_SECONDS}s"
    emit({"approval_resolved": {"id": request.id, "decision": request.decision}})
    if request.decision != "allow":
        return False, "the user denied this action"
    return True, "approved"


# ===== File journal =====

def journal_write(path: str, before: Optional[str], after: Optional[str], operation: str, conversation_id: Optional[str]) -> str:
    entry_id = str(uuid.uuid4())[:12]
    conn = get_db()
    conn.execute(
        """INSERT INTO file_journal
           (id, path, operation, before_content, after_content, reverted, conversation_id, created_at)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
        (entry_id, path, operation, before, after, conversation_id, _now()),
    )
    conn.commit()
    conn.close()
    return entry_id


def list_journal(limit: int = 50) -> List[Dict[str, Any]]:
    """Recent agent file operations with a unified diff for each."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM file_journal ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "path": r["path"],
            "operation": r["operation"],
            "reverted": bool(r["reverted"]),
            "conversation_id": r["conversation_id"],
            "created_at": r["created_at"],
            "diff": unified_diff(r["before_content"], r["after_content"], r["path"]),
        }
        for r in rows
    ]


def unified_diff(before: Optional[str], after: Optional[str], path: str) -> str:
    lines = difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        fromfile=f"a/{os.path.basename(path)}",
        tofile=f"b/{os.path.basename(path)}",
        n=3,
    )
    return "".join(lines)[:20000]


def revert_journal_entry(entry_id: str) -> Dict[str, Any]:
    """Restore a file to its pre-edit contents (deleting it if it was created)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM file_journal WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    if row is None:
        return {"success": False, "error": "journal entry not found"}
    if row["reverted"]:
        return {"success": False, "error": "already reverted"}

    path = row["path"]
    try:
        if row["before_content"] is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(row["before_content"])
    except OSError as exc:
        return {"success": False, "error": str(exc)}

    conn = get_db()
    conn.execute("UPDATE file_journal SET reverted = 1 WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return {"success": True, "path": path, "operation": row["operation"]}


# ===== Tool implementations =====

def _tool_read_file(path: str, **_) -> str:
    full = resolve(path)
    if not os.path.isfile(full):
        return f"error: no such file: {path}"
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(MAX_READ_CHARS + 1)
    except OSError as exc:
        return f"error: {exc}"
    truncated = len(content) > MAX_READ_CHARS
    numbered = "\n".join(
        f"{i + 1}\t{line}" for i, line in enumerate(content[:MAX_READ_CHARS].splitlines())
    )
    return numbered + ("\n... (truncated)" if truncated else "")


def _tool_write_file(path: str, content: str = "", conversation_id: Optional[str] = None, **_) -> str:
    full = resolve(path)
    before = None
    if os.path.isfile(full):
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                before = handle.read()
        except OSError:
            before = None
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        return f"error: {exc}"

    entry_id = journal_write(
        full, before, content, "create" if before is None else "edit", conversation_id
    )
    verb = "created" if before is None else "updated"
    return f"{verb} {path} ({len(content)} chars). Revert with journal entry {entry_id}."


def _tool_list_dir(path: str = "", **_) -> str:
    full = resolve(path)
    if not os.path.isdir(full):
        return f"error: not a directory: {path or '.'}"
    entries = []
    for name in sorted(os.listdir(full))[:MAX_LIST_ENTRIES]:
        child = os.path.join(full, name)
        if os.path.isdir(child):
            entries.append(f"{name}/")
        else:
            try:
                entries.append(f"{name} ({os.path.getsize(child)} bytes)")
            except OSError:
                entries.append(name)
    return "\n".join(entries) or "(empty directory)"


def _tool_search_files(pattern: str, path: str = "", **_) -> str:
    """Regex search across workspace files — the agent's grep."""
    root = resolve(path)
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"error: invalid pattern: {exc}"

    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            try:
                if os.path.getsize(full) > 2 * 1024 * 1024:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    for lineno, line in enumerate(handle, 1):
                        if regex.search(line):
                            rel = os.path.relpath(full, workspace_root())
                            matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                            if len(matches) >= MAX_GREP_MATCHES:
                                return "\n".join(matches) + "\n... (more matches truncated)"
            except OSError:
                continue
    return "\n".join(matches) or "no matches"


def _tool_run_command(command: str, **_) -> str:
    from . import terminal as terminal_mod

    result = terminal_mod.execute_command(command, cwd=workspace_root(), timeout=60)
    status = "ok" if result["success"] else f"exit {result['returncode']}"
    return f"[{status}]\n{result['output'][:8000]}"


def _tool_search_memory(query: str, **_) -> str:
    from . import memory as memory_mod

    results = memory_mod.search(query, limit=8)
    if not results:
        return "no memories matched"
    return "\n".join(f"- ({m['kind']}/{m['subject']}) {m['content']}" for m in results)


def _tool_search_documents(query: str, **_) -> str:
    from . import indexer as indexer_mod

    results = indexer_mod.search_documents(query, limit=5)["results"]
    if not results:
        return "no indexed documents matched"
    return "\n\n".join(
        f"{r['path']} (chunk {r['ordinal']}):\n{r['content'][:800]}" for r in results
    )


def _tool_search_conversations(query: str, **_) -> str:
    from . import search as search_mod

    results = search_mod.search_conversations(query, limit=5)["results"]
    if not results:
        return "no conversations matched"
    return "\n".join(
        f"[{r['timestamp'][:10]}] {r['role']}: {r['content'][:300]}" for r in results
    )


def _tool_create_reminder(title: str, due_at: str = "", description: str = "", **_) -> str:
    from . import reminders as reminders_mod

    reminder = reminders_mod.create_reminder(title=title, description=description, due_at=due_at or None)
    return f"created reminder {reminder['id']}: {title}" + (f" (due {due_at})" if due_at else "")


def _tool_create_note(title: str, content: str = "", folder: str = "", **_) -> str:
    from . import notes as notes_mod

    note = notes_mod.create_note(title=title, content=content, folder=folder or None)
    return f"created note {note['id']}: {title}"


# name -> (handler, mutating, risk, schema)
TOOLS: Dict[str, Dict[str, Any]] = {
    "read_file": {
        "handler": _tool_read_file,
        "mutating": False,
        "risk": "low",
        "description": "Read a UTF-8 text file from the workspace. Returns line-numbered content.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Workspace-relative file path"}},
            "required": ["path"],
        },
    },
    "write_file": {
        "handler": _tool_write_file,
        "mutating": True,
        "risk": "high",
        "description": "Write a file in the workspace, creating it if needed. The previous contents are journaled so the edit can be reverted.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path"},
                "content": {"type": "string", "description": "Full new file contents"},
            },
            "required": ["path", "content"],
        },
    },
    "list_dir": {
        "handler": _tool_list_dir,
        "mutating": False,
        "risk": "low",
        "description": "List the entries of a workspace directory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Workspace-relative directory, empty for the root"}},
        },
    },
    "search_files": {
        "handler": _tool_search_files,
        "mutating": False,
        "risk": "low",
        "description": "Regex-search the contents of workspace files. Returns path:line matches.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression"},
                "path": {"type": "string", "description": "Subdirectory to search, empty for the whole workspace"},
            },
            "required": ["pattern"],
        },
    },
    "run_command": {
        "handler": _tool_run_command,
        "mutating": True,
        "risk": "high",
        "description": "Run a shell command in the workspace root and return its output. Use for tests, builds, and git.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command to run"}},
            "required": ["command"],
        },
    },
    "search_memory": {
        "handler": _tool_search_memory,
        "mutating": False,
        "risk": "low",
        "description": "Search what Carrot knows about the user: preferences, decisions, ongoing projects.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "search_documents": {
        "handler": _tool_search_documents,
        "mutating": False,
        "risk": "low",
        "description": "Search the user's indexed local files (notes, papers, code, saved pages).",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "search_conversations": {
        "handler": _tool_search_conversations,
        "mutating": False,
        "risk": "low",
        "description": "Search past conversations with the user. Supports phrases like '3 months ago'.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "create_reminder": {
        "handler": _tool_create_reminder,
        "mutating": True,
        "risk": "medium",
        "description": "Create a reminder for the user.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due_at": {"type": "string", "description": "ISO 8601 timestamp"},
                "description": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    "create_note": {
        "handler": _tool_create_note,
        "mutating": True,
        "risk": "medium",
        "description": "Create a markdown note in the user's notes.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "folder": {"type": "string"},
            },
            "required": ["title"],
        },
    },
}


def namespaced(name: str) -> str:
    return f"{TOOL_PREFIX}__{name}"


def is_builtin(namespaced_name: str) -> bool:
    return namespaced_name.startswith(f"{TOOL_PREFIX}__")


def ollama_tools(enabled: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Built-in tools in Ollama's function-calling schema."""
    if not get_config().get("agent_tools_enabled", True):
        return []
    allowed = set(enabled) if enabled is not None else set(TOOLS)
    return [
        {
            "type": "function",
            "function": {
                "name": namespaced(name),
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for name, spec in TOOLS.items()
        if name in allowed
    ]


def _summarize_call(name: str, arguments: Dict[str, Any]) -> str:
    if name == "write_file":
        return f"Write {len(arguments.get('content', ''))} characters to {arguments.get('path', '?')}"
    if name == "run_command":
        return f"Run: {arguments.get('command', '?')}"
    if name == "create_reminder":
        return f"Create reminder: {arguments.get('title', '?')}"
    if name == "create_note":
        return f"Create note: {arguments.get('title', '?')}"
    return f"{name}({', '.join(f'{k}={v!r}'[:60] for k, v in arguments.items())})"


def run_tool(
    name: str,
    spec: Dict[str, Any],
    arguments: Dict[str, Any],
    conversation_id: Optional[str] = None,
    emit: Optional[Callable] = None,
    summary: Optional[str] = None,
) -> str:
    """Run one tool spec: approval gate, dispatch, error containment.

    Extension packs supply their own tool specs but must not reimplement any of
    this — a pack tool that writes a file has to hit the same approval prompt a
    built-in one does, so both go through here.
    """
    arguments = arguments or {}
    if spec.get("mutating") and get_config().get("agent_require_approval", True):
        approved, reason = _request_approval(
            name, arguments, summary or _summarize_call(name, arguments),
            spec.get("risk", "medium"), emit,
        )
        if not approved:
            return f"error: {reason}"

    started = time.time()
    try:
        result = spec["handler"](conversation_id=conversation_id, **arguments)
    except PermissionError as exc:
        return f"error: {exc}"
    except TypeError as exc:
        return f"error: bad arguments for {name}: {exc}"
    except Exception as exc:
        return f"error: {name} failed: {exc}"
    elapsed = time.time() - started
    if elapsed > 5:
        result = f"{result}\n\n(took {elapsed:.1f}s)"
    return result


def call(
    namespaced_name: str,
    arguments: Dict[str, Any],
    conversation_id: Optional[str] = None,
    emit: Optional[Callable] = None,
) -> str:
    """Execute a built-in tool, gating mutating calls behind user approval."""
    name = namespaced_name.split("__", 1)[-1]
    spec = TOOLS.get(name)
    if spec is None:
        return f"error: unknown tool {namespaced_name}"
    return run_tool(name, spec, arguments, conversation_id, emit)
