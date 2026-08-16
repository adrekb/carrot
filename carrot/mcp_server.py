"""Carrot as an MCP server: the other apps call *in*.

`mcp_client.py` is the outward half — Carrot calling somebody else's tools.
This is the half that made the interop story lopsided. "Open in Cursor" hands
you off and then Carrot is gone: the memory, the document index and the goals
stay behind in a window you just left, and the assistant on the other side
starts from nothing. Which is the whole problem Carrot exists to solve,
reintroduced by the one feature meant to bridge it.

So: a stdio MCP server. Cursor, VS Code, Claude Desktop and anything else that
speaks MCP add one line of config and can then ask Carrot what it knows. You
stay where you are. Carrot answers like a filesystem — no plugin on the other
side, which is the same promise `interop.py` already makes for Obsidian and
editors.

Three decisions worth stating.

**Read-only, and not negotiable.** Every tool here answers a question; none of
them writes, deletes, runs a command or touches the policy kernel. An MCP
client is an arbitrary program the user pointed at Carrot, driven by a model
Carrot has no visibility into and cannot put an approval prompt in front of —
there is no interactive channel on a stdio pipe. A gate nobody can answer is
not a gate, so the answer is that nothing needing one is exposed. Writing stays
inside Carrot, where the approval UI is.

**No new dependency.** MCP over stdio is newline-delimited JSON-RPC 2.0. That
is ~150 lines against the stdlib, and it keeps `pip install carrot` the same
size for people who never wire an editor to it.

**One implementation, exposed twice.** The search tools are the same handlers
the chat agent calls, imported rather than reimplemented. Two search paths that
drift is how "Carrot says one thing in the app and another in Cursor" starts.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, Callable, Dict, List

LOG = logging.getLogger(__name__)

# Matching mcp_client.py rather than chasing the newest spec. Both halves
# speaking the same revision is worth more here than a feature neither uses.
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "carrot"


# ===== Tools =====

def _search_memory(query: str = "", **_) -> str:
    from . import agent_tools

    return agent_tools._tool_search_memory(query)


def _search_documents(query: str = "", **_) -> str:
    from . import agent_tools

    return agent_tools._tool_search_documents(query)


def _search_conversations(query: str = "", **_) -> str:
    from . import agent_tools

    return agent_tools._tool_search_conversations(query)


def _list_goals(**_) -> str:
    """Through `render_status`, like chat.

    This used to list every row including proposals nobody had accepted, and
    read the deadline out of `metadata["due"]` — which is where it was guessed
    at before there was a column for it, so every deadline came back empty.
    Two implementations of "the user's goals" is how Carrot says one thing in
    the app and another in Cursor.
    """
    from . import goals as goals_mod

    return goals_mod.render_status()


def _list_reminders(**_) -> str:
    from . import reminders as reminders_mod

    rows = reminders_mod.list_reminders(completed=False, limit=50)
    if not rows:
        return "nothing outstanding"
    return "\n".join(
        f"- {r['title']}" + (f" (due {r['due_at'][:16]})" if r.get("due_at") else "")
        for r in rows
    )


def _query_schema(description: str) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {"query": {"type": "string", "description": description}},
        "required": ["query"],
    }


_NO_ARGS: Dict[str, Any] = {"type": "object", "properties": {}}

# The descriptions are written for a model on the other side that has never
# heard of Carrot, so each says what the store *is* rather than naming it.
TOOLS: Dict[str, Dict[str, Any]] = {
    "search_memory": {
        "handler": _search_memory,
        "description": "Search what Carrot knows about this user — their stated "
                       "preferences, decisions, projects and commitments. Use it "
                       "before assuming anything about how they work.",
        "inputSchema": _query_schema("What to look for, in plain language"),
    },
    "search_documents": {
        "handler": _search_documents,
        "description": "Search the user's own indexed files — PDFs, notes, papers, "
                       "saved pages — by meaning as well as by word.",
        "inputSchema": _query_schema("What to look for, in plain language"),
    },
    "search_conversations": {
        "handler": _search_conversations,
        "description": "Search the user's past conversations with Carrot, including "
                       "time expressions like 'last month'.",
        "inputSchema": _query_schema("What to look for, in plain language"),
    },
    "list_goals": {
        "handler": _list_goals,
        "description": "The user's open goals and their deadlines.",
        "inputSchema": _NO_ARGS,
    },
    "list_reminders": {
        "handler": _list_reminders,
        "description": "The user's outstanding reminders and when they are due.",
        "inputSchema": _NO_ARGS,
    },
}


def tool_list() -> List[Dict[str, Any]]:
    return [
        {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
        for name, spec in TOOLS.items()
    ]


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Run one tool and shape the MCP result envelope.

    Failures come back as ``isError`` content rather than as a JSON-RPC error,
    which is the distinction the protocol draws: the call was well-formed and
    the tool has something to say about why it did not work. A transport-level
    error would instead tell the client the server is broken, and it would stop
    sending — losing the user their editor integration over one bad query.
    """
    spec = TOOLS.get(name)
    if spec is None:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
    try:
        text = spec["handler"](**(arguments or {}))
    except Exception as exc:  # noqa: BLE001 - reported, never raised at the pipe
        LOG.exception("mcp tool %s failed", name)
        return {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
    return {"content": [{"type": "text", "text": text}]}


# ===== JSON-RPC over stdio =====

def handle(message: Dict[str, Any]) -> Dict[str, Any] | None:
    """Answer one request, or return None for a notification.

    Split out from the loop so the protocol is testable without pipes.
    """
    method = message.get("method")
    request_id = message.get("id")

    # A notification has no id and must not be answered. Replying to one is the
    # classic way to wedge a strict client.
    if request_id is None:
        return None

    def ok(result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": _version()},
        })
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        params = message.get("params") or {}
        return ok(call_tool(params.get("name", ""), params.get("arguments") or {}))
    if method == "ping":
        return ok({})
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("carrot")
    except Exception:  # noqa: BLE001 - running from a checkout without metadata
        return "0"


def serve(stdin=None, stdout=None) -> None:
    """Read requests from stdin, write responses to stdout, until EOF.

    Nothing else may ever be written to stdout — a stray print corrupts the
    stream and the client's only symptom is that Carrot's tools silently stop
    appearing. Logging goes to stderr, which the client shows in its MCP log.
    """
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            LOG.warning("dropped unparseable line")
            continue
        response = handle(message)
        if response is None:
            continue
        sink.write(json.dumps(response) + "\n")
        sink.flush()


def client_config(python: str = "") -> Dict[str, Any]:
    """The block to paste into an editor's MCP settings.

    Returned as data rather than printed so the Settings screen and the CLI
    show the same thing, and so the interpreter is the one actually running
    Carrot — an editor launching some other Python finds no `carrot` module and
    reports only that the server exited.
    """
    return {
        "mcpServers": {
            SERVER_NAME: {
                "command": python or sys.executable,
                "args": ["-m", "carrot.mcp_server"],
            }
        }
    }


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    serve()
