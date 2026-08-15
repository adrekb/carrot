"""Carrot answering as an MCP server, so editors can ask it what it knows.

The point of these is the *contract*: a client that Carrot never sees the
source of has to be able to handshake, enumerate and call without anything on
stdout that is not protocol.
"""
import io
import json

import pytest

from carrot import mcp_server


def _request(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


# ===== Handshake =====

def test_initialize_reports_tools_capability(isolated_db):
    result = mcp_server.handle(_request("initialize"))["result"]
    assert result["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "carrot"


def test_notifications_are_not_answered(isolated_db):
    # No id means no reply. Answering a notification wedges strict clients.
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_an_error_not_a_crash(isolated_db):
    response = mcp_server.handle(_request("does/not/exist"))
    assert response["error"]["code"] == -32601


# ===== Tools =====

def test_tool_list_is_complete_and_well_formed(isolated_db):
    tools = mcp_server.handle(_request("tools/list"))["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"search_memory", "search_documents", "search_conversations",
                     "list_goals", "list_reminders"}
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_every_exposed_tool_is_read_only(isolated_db):
    """The load-bearing assertion in this file.

    A stdio pipe has no interactive channel, so an approval prompt cannot be
    put in front of anything here — which means nothing that would need one may
    be exposed. This fails the moment somebody adds a writing tool, which is
    the point.
    """
    forbidden = ("write", "delete", "create", "run", "edit", "move", "install", "send")
    for name in mcp_server.TOOLS:
        assert not any(word in name for word in forbidden), name


def test_list_goals_reports_what_is_recorded(isolated_db):
    from carrot import goals as goals_mod

    goals_mod.create_goal("finish thesis", category="school")
    out = mcp_server.handle(
        _request("tools/call", {"name": "list_goals", "arguments": {}}))["result"]
    assert "finish thesis" in out["content"][0]["text"]
    assert not out.get("isError")


def test_empty_stores_say_so_rather_than_erroring(isolated_db):
    out = mcp_server.handle(
        _request("tools/call", {"name": "list_reminders", "arguments": {}}))["result"]
    assert "nothing outstanding" in out["content"][0]["text"]
    assert not out.get("isError")


def test_unknown_tool_is_content_not_transport_error(isolated_db):
    # isError, not a JSON-RPC error: one bad call must not read as "this
    # server is broken", or the client stops talking to it entirely.
    response = mcp_server.handle(_request("tools/call", {"name": "nope", "arguments": {}}))
    assert "error" not in response
    assert response["result"]["isError"] is True


def test_a_failing_tool_is_reported_not_raised(isolated_db, monkeypatch):
    def boom(**_):
        raise RuntimeError("index is on fire")

    monkeypatch.setitem(mcp_server.TOOLS["search_memory"], "handler", boom)
    result = mcp_server.call_tool("search_memory", {"query": "x"})
    assert result["isError"] is True
    assert "index is on fire" in result["content"][0]["text"]


# ===== The pipe =====

def test_serve_round_trips_over_stdio(isolated_db):
    incoming = "\n".join([
        json.dumps(_request("initialize")),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps(_request("tools/list", request_id=2)),
    ])
    out = io.StringIO()
    mcp_server.serve(stdin=io.StringIO(incoming), stdout=out)

    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    # Two requests in, two responses out — the notification produced nothing.
    assert [line["id"] for line in lines] == [1, 2]


def test_garbage_does_not_kill_the_stream(isolated_db):
    incoming = "not json\n\n" + json.dumps(_request("ping"))
    out = io.StringIO()
    mcp_server.serve(stdin=io.StringIO(incoming), stdout=out)
    assert json.loads(out.getvalue().strip())["id"] == 1


def test_client_config_points_at_the_running_interpreter(isolated_db):
    block = mcp_server.client_config(python="/opt/py/bin/python")
    entry = block["mcpServers"]["carrot"]
    assert entry["command"] == "/opt/py/bin/python"
    assert entry["args"] == ["-m", "carrot.mcp_server"]
