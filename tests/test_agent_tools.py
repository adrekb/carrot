"""Tests for built-in agent tools: sandboxing, approval, and the undo journal."""
import os
import re
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


# ===== Nothing is out of reach =====
#
# Each of these used to end at a cliff: the reply said something had been cut
# and offered no way to ask for the rest. The point of every assertion below is
# the *recovery path*, not the cut.

def _big_file(workspace, lines):
    (workspace / "big.txt").write_text("\n".join(f"line {i}" for i in range(1, lines + 1)))


def test_read_file_windows_and_says_how_to_continue(workspace):
    _big_file(workspace, 5000)
    first = agent_tools.call("carrot__read_file", {"path": "big.txt"})
    assert "1\tline 1" in first
    assert "of 5000" in first
    assert "offset=" in first


def test_read_file_offset_reaches_the_end(workspace):
    _big_file(workspace, 5000)
    tail = agent_tools.call("carrot__read_file", {"path": "big.txt", "offset": 4990})
    assert "5000\tline 5000" in tail
    # The last window is the end of the file, so it must not advertise another.
    assert "Continue with" not in tail


def test_read_file_walks_the_whole_file(workspace):
    _big_file(workspace, 5000)
    seen, offset, guard = set(), 1, 0
    while offset and guard < 50:
        guard += 1
        chunk = agent_tools.call("carrot__read_file", {"path": "big.txt", "offset": offset})
        seen.update(int(row.split("\t")[0]) for row in chunk.splitlines() if "\t" in row)
        match = re.search(r"offset=(\d+)", chunk)
        offset = int(match.group(1)) if match else 0
    assert seen == set(range(1, 5001))


def test_read_file_limit_is_honoured(workspace):
    _big_file(workspace, 5000)
    chunk = agent_tools.call("carrot__read_file", {"path": "big.txt", "offset": 10, "limit": 3})
    rows = [r for r in chunk.splitlines() if "\t" in r]
    assert len(rows) == 3
    assert rows[0].startswith("10\t")


def test_read_file_offset_past_the_end_says_so(workspace):
    _big_file(workspace, 10)
    assert "past the end" in agent_tools.call(
        "carrot__read_file", {"path": "big.txt", "offset": 99})


def test_search_files_spills_the_rest(workspace):
    (workspace / "many.py").write_text("\n".join("hit here" for _ in range(400)))
    matches = agent_tools.call("carrot__search_files", {"pattern": "hit"})
    assert "of 400 matches" in matches
    handle = re.search(r"read_file\(path='([^']+)'\)", matches).group(1)
    assert handle.startswith(agent_tools.SPILL_PREFIX)
    # The handle has to be readable through the ordinary tool, which is the
    # whole reason it is shaped like a path.
    everything = agent_tools.call("carrot__read_file", {"path": handle})
    assert "many.py:1:" in everything


def test_spill_handle_cannot_escape(workspace):
    escaped = agent_tools.call(
        "carrot__read_file", {"path": agent_tools.SPILL_PREFIX + "../../../etc/passwd"})
    assert escaped.startswith("error:")


def test_run_command_keeps_the_end_and_spills_the_whole(workspace):
    noisy = {"success": False, "returncode": 1,
             "output": "\n".join(f"noise {i}" for i in range(4000)) + "\nFAILED at the end"}
    with patch("carrot.terminal.execute_command", return_value=noisy):
        out = agent_tools.call("carrot__run_command", {"command": "pytest"})
    # The summary of a failing run is its last line. Cutting from the front
    # was throwing away the only part worth reading.
    assert "FAILED at the end" in out
    assert "omitted from the middle" in out
    handle = re.search(r"read_file\(path='([^']+)'\)", out).group(1)
    assert "noise 0" in agent_tools.call("carrot__read_file", {"path": handle})


def test_run_command_pays_for_its_notice_inside_the_cap(workspace):
    """The reply never exceeds the limit it is enforcing.

    Spending the whole budget on the excerpt and appending the notice
    afterwards makes the reply longer than the cap — and for output barely
    over it, longer than the original, which is truncation that costs context
    instead of saving it.
    """
    noisy = {"success": True, "returncode": 0, "output": "x" * (agent_tools.MAX_COMMAND_CHARS + 50)}
    with patch("carrot.terminal.execute_command", return_value=noisy):
        out = agent_tools.call("carrot__run_command", {"command": "noisy"})
    body = out.split("\n", 1)[1]  # drop the "[ok]" status line
    assert len(body) <= agent_tools.MAX_COMMAND_CHARS


def test_unknown_tool(workspace):
    assert "unknown tool" in agent_tools.call("carrot__does_not_exist", {})


def test_bad_arguments_are_reported(workspace):
    # "bad-call:" rather than "error:" since the missing-argument check moved
    # ahead of dispatch: the model has to be able to tell a call it should
    # repeat correctly from a tool that is broken. Both are still a reported
    # message rather than a raised exception, which is what this pins.
    result = agent_tools.call("carrot__read_file", {})
    assert result.startswith("bad-call:")
    assert "path" in result


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
    # "not-run:" is the contract with the model — it is what distinguishes a
    # call that is finished from one worth retrying. The exact adjective is
    # wording and differs per outcome; this is the part that must hold.
    assert result.startswith("not-run:")
    assert "approval" in result
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
    assert result.startswith("not-run:")
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
        # Driven by a timeout, which is what this hint is for. It used to be
        # driven by a refusal only because both shared one template; offering
        # "here is how to stop asking" to someone who just deliberately said no
        # is the wrong reply to the wrong event.
        monkeypatch.setattr(agent_tools, "_request_approval",
                            lambda *a, **k: (False, agent_tools.timeout_reason()))
        result = agent_tools.run_tool("write_file", self.spec(),
                                      {"path": "snake.py", "content": "x"})
        assert "Settings" in result

    def test_the_reason_survives_into_the_message(self, isolated_db, monkeypatch):
        monkeypatch.setattr(agent_tools, "_request_approval",
                            lambda *a, **k: (False, agent_tools.DENIED_REASON))
        result = agent_tools.run_tool("write_file", self.spec(),
                                      {"path": "snake.py", "content": "x"})
        assert "refused" in result


class TestARefusedCallIsNotReportedAsDone:
    """A denied write_file was answered with "I have created the file
    hello.txt". Nothing had been created.

    The refusal reached the model correctly — the failure is that the message
    only said the call did not run, and left it to infer what to say. It
    narrated the success it had been about to describe. So every one of these
    messages now states that nothing changed and forbids claiming otherwise,
    and a refusal no longer borrows the timeout's "waiting on your approval",
    which was simply untrue: nothing is pending and the answer will not change.
    """

    def spec(self):
        return {"handler": lambda **kw: "wrote it",
                "mutating": True, "risk": "high"}

    def outcomes(self):
        return [agent_tools.DENIED_REASON,
                agent_tools.NO_CHANNEL_REASON,
                agent_tools.timeout_reason()]

    def test_no_outcome_lets_the_model_claim_it_happened(self, isolated_db, monkeypatch):
        for reason in self.outcomes():
            monkeypatch.setattr(agent_tools, "_request_approval",
                                lambda *a, _r=reason, **k: (False, _r))
            result = agent_tools.run_tool("write_file", self.spec(),
                                          {"path": "snake.py", "content": "x"})
            lowered = result.lower()
            assert "nothing was changed" in lowered, reason
            assert "do not tell the user you did it" in lowered, reason

    def test_a_refusal_is_not_described_as_pending(self, isolated_db, monkeypatch):
        monkeypatch.setattr(agent_tools, "_request_approval",
                            lambda *a, **k: (False, agent_tools.DENIED_REASON))
        result = agent_tools.run_tool("write_file", self.spec(),
                                      {"path": "snake.py", "content": "x"})
        assert "waiting" not in result.lower(), (
            "a refused call is not waiting on anything — saying so invites the "
            "model to tell the user it is about to happen"
        )

    def test_a_timeout_still_is_described_as_pending(self, isolated_db, monkeypatch):
        # The distinction only matters if the other side of it still holds.
        monkeypatch.setattr(agent_tools, "_request_approval",
                            lambda *a, **k: (False, agent_tools.timeout_reason()))
        result = agent_tools.run_tool("write_file", self.spec(),
                                      {"path": "snake.py", "content": "x"})
        assert "waiting" in result.lower()

    def test_an_unrecognised_reason_still_gets_the_guard(self, isolated_db, monkeypatch):
        # A new outcome added later must not silently fall through to a message
        # that permits claiming success.
        monkeypatch.setattr(agent_tools, "_request_approval",
                            lambda *a, **k: (False, "something nobody has written yet"))
        result = agent_tools.run_tool("write_file", self.spec(),
                                      {"path": "snake.py", "content": "x"})
        assert "nothing was changed" in result.lower()
        assert "do not tell the user you did it" in result.lower()


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


class TestTheAgentCanRemoveThingsToo:
    """"Can it edit, add and delete files?" — the answer used to be two of three.

    There was no delete and no rename at all, which means most refactors could
    not be finished: the dead module stays and the user is asked to remove it
    by hand, in the one tool that was supposed to do the work.
    """

    def test_a_file_can_be_deleted(self, workspace):
        (workspace / "dead.py").write_text("obsolete")
        result = agent_tools.call("carrot__delete_file", {"path": "dead.py"}, None, None)
        assert not (workspace / "dead.py").exists()
        assert "deleted" in result

    def test_a_delete_is_revertable(self, workspace):
        (workspace / "dead.py").write_text("obsolete")
        result = agent_tools.call("carrot__delete_file", {"path": "dead.py"}, None, None)
        entry = result.rsplit(" ", 1)[-1].rstrip(".")
        assert agent_tools.revert_journal_entry(entry)["success"]
        assert (workspace / "dead.py").read_text() == "obsolete"

    def test_deleting_a_directory_is_refused(self, workspace):
        (workspace / "src").mkdir()
        result = agent_tools.call("carrot__delete_file", {"path": "src"}, None, None)
        assert result.startswith("error:")
        assert (workspace / "src").exists()

    def test_deleting_outside_the_workspace_is_refused(self, workspace):
        result = agent_tools.call(
            "carrot__delete_file", {"path": "../../etc/hosts"}, None, None)
        assert result.startswith("error:")

    def test_a_missing_file_is_an_error_not_a_silent_success(self, workspace):
        result = agent_tools.call("carrot__delete_file", {"path": "ghost.py"}, None, None)
        assert result.startswith("error:")

    def test_a_file_can_be_renamed(self, workspace):
        (workspace / "old.py").write_text("content")
        agent_tools.call("carrot__move_file",
                         {"path": "old.py", "to": "new.py"}, None, None)
        assert not (workspace / "old.py").exists()
        assert (workspace / "new.py").read_text() == "content"

    def test_a_move_never_silently_overwrites(self, workspace):
        (workspace / "a.py").write_text("keep me")
        (workspace / "b.py").write_text("do not lose me")
        result = agent_tools.call("carrot__move_file",
                                  {"path": "a.py", "to": "b.py"}, None, None)
        assert result.startswith("error:")
        assert (workspace / "b.py").read_text() == "do not lose me"

    def test_a_move_cannot_escape_the_workspace(self, workspace):
        (workspace / "a.py").write_text("x")
        result = agent_tools.call("carrot__move_file",
                                  {"path": "a.py", "to": "../../escaped.py"}, None, None)
        assert result.startswith("error:")
        assert (workspace / "a.py").exists()

    def test_both_removals_ask_before_acting(self, isolated_db):
        for name in ("delete_file", "move_file"):
            assert agent_tools.TOOLS[name]["mutating"] is True
            assert agent_tools.TOOLS[name]["risk"] == "high"

    def test_plan_mode_cannot_reach_them(self):
        from carrot import coder

        names = ["carrot__delete_file", "carrot__move_file", "carrot__read_file"]
        allowed = coder.tools_for_mode(names, coder.MODE_PLAN)
        assert allowed == ["carrot__read_file"]


class TestAMalformedCallIsRecoverable:
    """Asked for a snake game, the model called write_file with the whole
    program in `content` and no `path`, and the turn died.

    What came back was "_tool_write_file() missing 1 required positional
    argument: 'path'" — a private function name and a Python concept, from a
    raw TypeError. The model did not retry; it answered "the notes do not
    answer this question because nothing was gathered".

    This is the one failure in run_tool that *should* be retried, so unlike a
    refusal it says to call again. It has to name the argument in the tool's
    own vocabulary to be actionable, and it still must not let the model claim
    the write happened.
    """

    def spec(self):
        return {
            "handler": lambda path, content, **kw: "wrote it",
            "mutating": True,
            "risk": "high",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"},
                               "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        }

    def test_a_missing_argument_is_named(self, isolated_db):
        result = agent_tools.run_tool("write_file", self.spec(), {"content": "x"})
        assert "path" in result
        assert "_tool_write_file" not in result, "leaks the private handler name"
        assert "positional argument" not in result, "leaks the Python signature"

    def test_it_says_to_call_again(self, isolated_db):
        # The opposite of the approval messages, which must stop the retry.
        result = agent_tools.run_tool("write_file", self.spec(), {"content": "x"})
        assert "again" in result.lower()

    def test_it_still_cannot_be_reported_as_done(self, isolated_db):
        result = agent_tools.run_tool("write_file", self.spec(), {"content": "x"})
        assert "nothing was written" in result.lower()
        assert "do not tell the user it was done" in result.lower()

    def test_an_empty_string_counts_as_supplied(self, isolated_db):
        # Writing an empty file is a real thing to want; it is not a missing
        # argument, and rejecting it would break truncating a file.
        assert agent_tools._missing_arguments(
            self.spec(), {"path": "a.py", "content": ""}) == []

    def test_a_complete_call_is_not_flagged(self, isolated_db):
        assert agent_tools._missing_arguments(
            self.spec(), {"path": "a.py", "content": "x"}) == []

    def test_the_user_is_not_asked_to_approve_a_call_that_cannot_run(self, isolated_db):
        # The gate used to come first, so a write with no path still raised a
        # prompt — a question with no good answer either way.
        prompts = []
        agent_tools.run_tool("write_file", self.spec(), {"content": "x"},
                             emit=prompts.append)
        assert not [p for p in prompts if "approval_request" in p]
