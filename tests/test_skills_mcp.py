"""Tests for SKILL.md skills, MCP server config, and the chat tool-call loop."""
import os
import sys
import pytest

STUB_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub_mcp_server.py")


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    from carrot import skills as skills_mod
    d = str(tmp_path / "skills")
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", d)
    return d


@pytest.fixture
def mcp_config(tmp_path, monkeypatch):
    from carrot import mcp_client as mcp_mod
    path = str(tmp_path / "mcp.json")
    monkeypatch.setattr(mcp_mod, "MCP_CONFIG_PATH", path)
    return path


# ===== skills CRUD =====

def test_skills_crud_via_api(client, skills_dir):
    assert client.get("/api/skills").json() == []

    created = client.post("/api/skills", json={
        "name": "Code Reviewer",
        "description": "Reviews code",
        "instructions": "Be strict about edge cases.",
    }).json()
    assert created["slug"] == "code-reviewer"

    listing = client.get("/api/skills").json()
    assert len(listing) == 1
    assert listing[0]["name"] == "Code Reviewer"

    fetched = client.get("/api/skills/code-reviewer").json()
    assert fetched["instructions"] == "Be strict about edge cases."

    assert client.delete("/api/skills/code-reviewer").json()["status"] == "deleted"
    assert client.get("/api/skills").json() == []
    assert client.get("/api/skills/code-reviewer").status_code == 404


def test_skill_requires_name(client, skills_dir):
    resp = client.post("/api/skills", json={"name": "  ", "description": "", "instructions": ""})
    assert resp.status_code == 400


def test_skill_injected_into_chat(client, skills_dir):
    """A selected skill adds a system message to the model history."""
    from carrot import app as app_mod
    client.post("/api/skills", json={
        "name": "Poet", "description": "", "instructions": "Answer in rhyme.",
    })
    captured = {}

    class SpyClient:
        default_model = "gemma4:e4b"
        def is_available(self):
            return True
        def chat(self, messages, model=None, stream=False):
            captured["messages"] = messages
            return "roses are red"

    resp = client.post("/api/chat", json={"message": "hi", "skill": "poet"})
    assert resp.status_code == 200
    # The non-streaming path builds history via _prepare_history; check the skill system msg.
    history, skill = app_mod._prepare_history(
        {"messages": []}, "hi", "poet",
    )
    assert skill["slug"] == "poet"
    assert history[0]["role"] == "system"
    assert "Answer in rhyme." in history[0]["content"]


# ===== MCP server config CRUD =====

def test_mcp_server_crud_via_api(client, mcp_config):
    assert client.get("/api/mcp/servers").json()["servers"] == {}

    client.post("/api/mcp/servers", json={
        "name": "weather", "command": "python", "args": ["w.py"], "enabled": True,
    })
    servers = client.get("/api/mcp/servers").json()["servers"]
    assert "weather" in servers
    assert servers["weather"]["command"] == "python"

    client.post("/api/mcp/servers/weather/enable", json={"enabled": False})
    servers = client.get("/api/mcp/servers").json()["servers"]
    assert servers["weather"]["enabled"] is False

    assert client.delete("/api/mcp/servers/weather").json()["status"] == "deleted"
    assert client.get("/api/mcp/servers").json()["servers"] == {}


def test_mcp_server_requires_fields(client, mcp_config):
    resp = client.post("/api/mcp/servers", json={"name": "", "command": "", "args": []})
    assert resp.status_code == 400


# ===== MCP stdio discovery (real subprocess) =====

def test_mcp_discover_tools_with_stub(client, mcp_config):
    client.post("/api/mcp/servers", json={
        "name": "stub", "command": sys.executable, "args": [STUB_SERVER], "enabled": True,
    })
    data = client.get("/api/mcp/tools").json()
    assert data["stub"]["error"] is None
    tool_names = [t["name"] for t in data["stub"]["tools"]]
    assert "echo" in tool_names


def test_mcp_call_namespaced_tool(mcp_config):
    from carrot import mcp_client as mcp_mod
    mcp_mod.add_server("stub", sys.executable, [STUB_SERVER], enabled=True)
    result = mcp_mod.call_namespaced_tool("stub__echo", {"text": "hello"})
    assert result == "echo: hello"


# ===== chat tool-call loop =====

def test_agentic_chat_tool_loop(monkeypatch):
    """Model emits a tool call, it runs via MCP, result feeds a final answer."""
    from carrot import app as app_mod
    from carrot import mcp_client as mcp_mod

    monkeypatch.setattr(mcp_mod, "ollama_tools", lambda: [{
        "type": "function",
        "function": {"name": "stub__echo", "description": "", "parameters": {"type": "object"}},
    }])
    calls = []
    monkeypatch.setattr(
        mcp_mod, "call_namespaced_tool",
        lambda name, args: calls.append((name, args)) or "echo: hi",
    )

    class ToolClient:
        default_model = "gemma4:e4b"
        def __init__(self):
            self.round = 0
        def chat_stream_events(self, messages, model=None, tools=None):
            self.round += 1
            if self.round == 1:
                yield {"type": "tool_calls", "calls": [
                    {"function": {"name": "stub__echo", "arguments": {"text": "hi"}}},
                ]}
            else:
                yield {"type": "content", "text": "done: "}
                yield {"type": "content", "text": "echo: hi"}

    events = list(app_mod._agentic_chat_events(ToolClient(), [{"role": "user", "content": "hi"}], "m"))
    tool_events = [e["tool"] for e in events if "tool" in e]
    result_events = [e["tool_result"] for e in events if "tool_result" in e]
    chunks = "".join(e["chunk"] for e in events if "chunk" in e)
    final = next(e["_final_text"] for e in events if "_final_text" in e)

    assert tool_events == [{"name": "stub__echo", "args": {"text": "hi"}}]
    assert result_events[0]["result"] == "echo: hi"
    assert calls == [("stub__echo", {"text": "hi"})]
    assert chunks == "done: echo: hi"
    assert final == "done: echo: hi"


def test_agentic_chat_no_tools_passthrough(monkeypatch):
    """With no MCP tools, the loop streams content straight through."""
    from carrot import app as app_mod
    from carrot import mcp_client as mcp_mod
    monkeypatch.setattr(mcp_mod, "ollama_tools", lambda: [])

    class PlainClient:
        default_model = "gemma4:e4b"
        def chat_stream_events(self, messages, model=None, tools=None):
            assert tools is None
            yield {"type": "content", "text": "plain answer"}

    events = list(app_mod._agentic_chat_events(PlainClient(), [], "m"))
    assert "".join(e["chunk"] for e in events if "chunk" in e) == "plain answer"
    assert not any("tool" in e for e in events)
