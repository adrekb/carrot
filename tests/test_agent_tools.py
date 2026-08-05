"""Tests for built-in agent tools: sandboxing, approval, and the undo journal."""
import os
import threading
from unittest.mock import patch

import pytest

from carrot import agent_tools, config


@pytest.fixture
def workspace(tmp_path, isolated_db):
    """A workspace root the agent tools operate inside."""
    root = tmp_path / "workspace"
    root.mkdir()
    config.set_config("code_workspace_dir", str(root))
    config.set_config("agent_require_approval", False)
    agent_tools.reset_session_approvals()
    return root


# ===== Path containment =====

def test_resolve_stays_inside_workspace(workspace):
    assert agent_tools.resolve("a/b.txt").startswith(str(workspace))


@pytest.mark.parametrize("escape", ["../outside.txt", "a/../../outside.txt", "/etc/passwd"])
def test_resolve_rejects_escapes(workspace, escape):
    with pytest.raises(PermissionError):
        agent_tools.resolve(escape)


def test_read_file_rejects_traversal(workspace):
    result = agent_tools.call("carrot__read_file", {"path": "../../../etc/passwd"})
    assert result.startswith("error:")
    assert "escapes the workspace" in result


def test_write_file_rejects_traversal(workspace):
    result = agent_tools.call("carrot__write_file", {"path": "../evil.txt", "content": "x"})
    assert result.startswith("error:")
    assert not (workspace.parent / "evil.txt").exists()


# ===== File tools =====

def test_write_then_read(workspace):
    agent_tools.call("carrot__write_file", {"path": "notes.txt", "content": "line one\nline two"})
    assert (workspace / "notes.txt").read_text() == "line one\nline two"

    read_back = agent_tools.call("carrot__read_file", {"path": "notes.txt"})
    assert "1\tline one" in read_back, "content is line-numbered"


def test_write_creates_parent_directories(workspace):
    agent_tools.call("carrot__write_file", {"path": "a/b/c.txt", "content": "deep"})
    assert (workspace / "a" / "b" / "c.txt").read_text() == "deep"


def test_read_missing_file(workspace):
    assert "no such file" in agent_tools.call("carrot__read_file", {"path": "nope.txt"})


def test_list_dir(workspace):
    (workspace / "sub").mkdir()
    (workspace / "a.txt").write_text("hi")
    listing = agent_tools.call("carrot__list_dir", {"path": ""})
    assert "sub/" in listing
    assert "a.txt" in listing


def test_search_files(workspace):
    (workspace / "a.py").write_text("def alpha():\n    pass\n")
    (workspace / "b.py").write_text("def beta():\n    pass\n")
    matches = agent_tools.call("carrot__search_files", {"pattern": r"def \w+"})
    assert "a.py:1" in matches
    assert "b.py:1" in matches


def test_search_files_invalid_pattern(workspace):
    assert "invalid pattern" in agent_tools.call("carrot__search_files", {"pattern": "([unclosed"})


def test_unknown_tool(workspace):
    assert "unknown tool" in agent_tools.call("carrot__does_not_exist", {})


def test_bad_arguments_are_reported(workspace):
    assert agent_tools.call("carrot__read_file", {}).startswith("error:")


# ===== Journal and undo =====

def test_edit_is_journaled_with_a_diff(workspace):
    (workspace / "f.txt").write_text("original\n")
    agent_tools.call("carrot__write_file", {"path": "f.txt", "content": "modified\n"})

    entries = agent_tools.list_journal()
    assert len(entries) == 1
    assert entries[0]["operation"] == "edit"
    assert "-original" in entries[0]["diff"]
    assert "+modified" in entries[0]["diff"]


def test_revert_restores_previous_contents(workspace):
    (workspace / "f.txt").write_text("original\n")
    agent_tools.call("carrot__write_file", {"path": "f.txt", "content": "modified\n"})

    entry = agent_tools.list_journal()[0]
    assert agent_tools.revert_journal_entry(entry["id"])["success"] is True
    assert (workspace / "f.txt").read_text() == "original\n"


def test_revert_deletes_a_created_file(workspace):
    agent_tools.call("carrot__write_file", {"path": "new.txt", "content": "hello"})
    entry = agent_tools.list_journal()[0]
    assert entry["operation"] == "create"

    agent_tools.revert_journal_entry(entry["id"])
    assert not (workspace / "new.txt").exists()


def test_revert_is_not_repeatable(workspace):
    agent_tools.call("carrot__write_file", {"path": "n.txt", "content": "x"})
    entry_id = agent_tools.list_journal()[0]["id"]
    agent_tools.revert_journal_entry(entry_id)
    assert agent_tools.revert_journal_entry(entry_id)["error"] == "already reverted"


def test_revert_unknown_entry(workspace):
    assert agent_tools.revert_journal_entry("nope")["success"] is False


# ===== Approval gate =====

def test_readonly_tools_never_prompt(workspace):
    config.set_config("agent_require_approval", True)
    prompts = []
    agent_tools.call("carrot__list_dir", {"path": ""}, emit=prompts.append)
    assert prompts == []


def test_mutating_tool_denied_without_a_channel(workspace):
    """No UI attached means no way to ask, so the call is refused, not hung."""
    config.set_config("agent_require_approval", True)
    result = agent_tools.call("carrot__write_file", {"path": "x.txt", "content": "y"})
    assert "approval required" in result
    assert not (workspace / "x.txt").exists()


def _call_with_decision(decision, workspace, remember=False):
    """Run a mutating tool on a thread and answer its approval prompt."""
    prompts = []
    result_box = {}

    def emit(event):
        prompts.append(event)
        request = event.get("approval_request")
        if request:
            agent_tools.resolve_approval(request["id"], decision, remember=remember)

    def work():
        result_box["result"] = agent_tools.call(
            "carrot__write_file", {"path": "gated.txt", "content": "written"}, emit=emit
        )

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(timeout=10)
    return result_box.get("result"), prompts


def test_approval_allows_the_write(workspace):
    config.set_config("agent_require_approval", True)
    result, prompts = _call_with_decision("allow", workspace)
    assert "created gated.txt" in result
    assert (workspace / "gated.txt").read_text() == "written"
    assert any("approval_request" in p for p in prompts)
    assert any(p.get("approval_resolved", {}).get("decision") == "allow" for p in prompts)


def test_denial_blocks_the_write(workspace):
    config.set_config("agent_require_approval", True)
    result, _ = _call_with_decision("deny", workspace)
    assert "denied" in result
    assert not (workspace / "gated.txt").exists()


def test_remembered_approval_skips_the_second_prompt(workspace):
    config.set_config("agent_require_approval", True)
    _call_with_decision("allow", workspace, remember=True)

    prompts = []
    result = agent_tools.call(
        "carrot__write_file", {"path": "again.txt", "content": "z"}, emit=prompts.append
    )
    assert "created again.txt" in result
    assert prompts == [], "the tool was allowed for the session"


def test_approval_prompt_summarizes_the_action(workspace):
    summary = agent_tools._summarize_call("run_command", {"command": "rm -rf /tmp/x"})
    assert "rm -rf /tmp/x" in summary


def test_resolve_unknown_approval():
    assert agent_tools.resolve_approval("nope", "allow") is False


# ===== Tool schema =====

def test_tools_are_namespaced_and_well_formed(workspace):
    schemas = agent_tools.ollama_tools()
    names = {t["function"]["name"] for t in schemas}
    assert "carrot__read_file" in names
    assert all(t["function"]["description"] for t in schemas)
    assert all("parameters" in t["function"] for t in schemas)


def test_tools_can_be_disabled(workspace):
    config.set_config("agent_tools_enabled", False)
    assert agent_tools.ollama_tools() == []


def test_is_builtin_discriminates_from_mcp():
    assert agent_tools.is_builtin("carrot__read_file") is True
    assert agent_tools.is_builtin("weather__forecast") is False


# ===== Recall tools =====

def test_search_memory_tool(workspace, monkeypatch):
    from carrot import memory, vectors

    monkeypatch.setattr(vectors, "search_text", lambda *a, **k: [])
    memory.create("preference", "editor", "The user prefers Neovim.")
    assert "Neovim" in agent_tools.call("carrot__search_memory", {"query": "editor"})


def test_create_reminder_tool(workspace):
    result = agent_tools.call("carrot__create_reminder", {"title": "Pay rent"})
    assert "created reminder" in result

    from carrot import reminders
    assert any(r["title"] == "Pay rent" for r in reminders.list_reminders())


# ===== API =====

def test_agent_tools_endpoint(client):
    data = client.get("/api/agent/tools").json()
    assert any(t["name"] == "write_file" and t["mutating"] for t in data["tools"])


def test_journal_endpoint_and_revert(client, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    config.set_config("code_workspace_dir", str(root))
    config.set_config("agent_require_approval", False)
    (root / "f.txt").write_text("before\n")
    agent_tools.call("carrot__write_file", {"path": "f.txt", "content": "after\n"})

    entries = client.get("/api/agent/journal").json()["entries"]
    assert entries and entries[0]["operation"] == "edit"

    assert client.post(f"/api/agent/journal/{entries[0]['id']}/revert").status_code == 200
    assert (root / "f.txt").read_text() == "before\n"


def test_revert_unknown_entry_endpoint(client):
    assert client.post("/api/agent/journal/nope/revert").status_code == 400


def test_approval_endpoints(client):
    assert client.get("/api/agent/approvals").json()["pending"] == []
    assert client.post("/api/agent/approvals/nope", json={"decision": "allow"}).status_code == 404


# ===== App-level tool plumbing =====

def test_run_tool_forwards_approval_prompts(isolated_db, tmp_path):
    """The stream must stay live while a tool is blocked on approval."""
    import threading

    from carrot import app as app_mod

    config.set_config("code_workspace_dir", str(tmp_path))
    config.set_config("agent_require_approval", True)
    agent_tools.reset_session_approvals()

    events, result = [], {}

    def drain():
        for event in app_mod._run_tool(
            "carrot__write_file", {"path": "x.txt", "content": "hi"}, "conv-1"
        ):
            if "_tool_result" in event:
                result["value"] = event["_tool_result"]
            else:
                events.append(event)

    thread = threading.Thread(target=drain)
    thread.start()

    # Wait for the prompt to surface, then answer it.
    for _ in range(100):
        pending = agent_tools.pending_approvals()
        if pending:
            agent_tools.resolve_approval(pending[0]["id"], "allow")
            break
        threading.Event().wait(0.05)
    thread.join(timeout=10)

    assert any("approval_request" in e for e in events)
    assert "created x.txt" in result["value"]
    assert (tmp_path / "x.txt").read_text() == "hi"


def test_run_tool_dispatches_mcp_by_name(isolated_db, monkeypatch):
    """Names without the carrot__ prefix go to MCP, not the built-ins."""
    from carrot import app as app_mod
    from carrot import mcp_client as mcp_mod

    monkeypatch.setattr(mcp_mod, "call_namespaced_tool", lambda name, args: f"mcp:{name}")
    results = [e for e in app_mod._run_tool("weather__forecast", {}, None) if "_tool_result" in e]
    assert results[0]["_tool_result"] == "mcp:weather__forecast"


def test_run_tool_reports_exceptions_as_results(isolated_db, monkeypatch):
    """A throwing tool becomes an error string the model can react to."""
    from carrot import app as app_mod
    from carrot import mcp_client as mcp_mod

    def explode(name, args):
        raise RuntimeError("server gone")

    monkeypatch.setattr(mcp_mod, "call_namespaced_tool", explode)
    results = [e for e in app_mod._run_tool("weather__forecast", {}, None) if "_tool_result" in e]
    assert "server gone" in results[0]["_tool_result"]


class TestAnUnansweredPromptDoesNotBecomeALoop:
    """The reported failure: "just do it all with minimal input from my end",
    followed by two identical write_file calls, each dying at the timeout.

    The prompt is not the bug. Returning `error:` for it is: a model reading
    "error: approval timed out" concludes the tool is flaky and calls it again,
    which stops at the same prompt, which times out again. Four minutes gone
    and a file that was never written.
    """

    def spec(self):
        return {"handler": lambda **kw: "wrote it",
                "mutating": True, "risk": "high"}

    def test_a_timeout_is_not_reported_as_a_tool_error(self, isolated_db, monkeypatch):
        monkeypatch.setattr(agent_tools, "_request_approval",
                            lambda *a, **k: (False, "approval timed out after 600s"))
        result = agent_tools.run_tool("write_file", self.spec(),
                                      {"path": "snake.py", "content": "x"})
        assert not result.startswith("error:")

    def test_the_model_is_told_not_to_call_it_again(self, isolated_db, monkeypatch):
        monkeypatch.setattr(agent_tools, "_request_approval",
                            lambda *a, **k: (False, "approval timed out after 600s"))
        result = agent_tools.run_tool("write_file", self.spec(),
                                      {"path": "snake.py", "content": "x"})
        assert "again" in result
        assert "write_file" in result

    def test_the_user_is_told_how_to_stop_being_asked(self, isolated_db, monkeypatch):
        monkeypatch.setattr(agent_tools, "_request_approval",
                            lambda *a, **k: (False, "the user denied this action"))
        result = agent_tools.run_tool("write_file", self.spec(),
                                      {"path": "snake.py", "content": "x"})
        assert "Settings" in result

    def test_the_reason_survives_into_the_message(self, isolated_db, monkeypatch):
        monkeypatch.setattr(agent_tools, "_request_approval",
                            lambda *a, **k: (False, "the user denied this action"))
        result = agent_tools.run_tool("write_file", self.spec(),
                                      {"path": "snake.py", "content": "x"})
        assert "denied" in result


class TestRiskIsAboutTheCallNotTheTool:
    """Creating a file destroys nothing. Overwriting one can destroy a day.

    ``write_file`` was flat "high", so a brand-new snake_game.py in an empty
    workspace raised the same red prompt as flattening a file with work in it.
    Asking hardest about the safest thing a tool does is how a user who said
    "just do it" ends up staring at a modal.
    """

    def spec(self):
        return {"handler": lambda **kw: "ok", "mutating": True, "risk": "high"}

    def test_creating_a_new_file_is_low_risk(self, isolated_db):
        assert agent_tools._risk_of(
            "write_file", self.spec(), {"path": "brand_new.py"}) == "low"

    def test_overwriting_an_existing_file_is_still_high_risk(self, isolated_db):
        path = os.path.join(agent_tools.workspace_root(), "existing.py")
        with open(path, "w") as handle:
            handle.write("a day of work")
        assert agent_tools._risk_of(
            "write_file", self.spec(), {"path": "existing.py"}) == "high"

    def test_other_tools_keep_their_declared_risk(self, isolated_db):
        assert agent_tools._risk_of(
            "run_command", {"risk": "high"}, {"command": "rm -rf /"}) == "high"

    def test_a_path_that_escapes_the_workspace_is_not_downgraded(self, isolated_db):
        # resolve() raises for these; a raise must never become "low".
        assert agent_tools._risk_of(
            "write_file", self.spec(), {"path": "../../etc/passwd"}) == "high"

    def test_the_prompt_a_user_actually_sees_uses_the_call_risk(self, isolated_db):
        seen = {}

        def capture(tool, args, summary, risk, emit):
            seen["risk"] = risk
            return True, "approved"

        with patch.object(agent_tools, "_request_approval", capture):
            agent_tools.run_tool("write_file", self.spec(),
                                 {"path": "snake_game.py", "content": "import pygame"})
        assert seen["risk"] == "low"
