"""Several read-only investigations of one codebase, at the same time.

Understanding an unfamiliar project is mostly breadth, and breadth done in one
conversation is done in sequence — each question's file dumps pushed into the
same context window until the first answer has been compacted into a sentence.
Split up, each part keeps its own context and returns a paragraph instead of
the forty files it read.

Most of these are about the two ways that becomes dangerous rather than
useful: agents that can write racing each other on the same files with no way
for the user to tell which one is asking, and fan-out — three agents each
spawning three is nine conversations against a metered endpoint, from one
sentence somebody typed.
"""
import threading
import time

import pytest

from carrot import agent_tools, subagents


class FakeRoute:
    provider, model, local = "anthropic", "claude-opus-5", False


@pytest.fixture(autouse=True)
def _hosted_route(monkeypatch):
    monkeypatch.setattr(subagents.router_mod, "route", lambda *a, **k: FakeRoute())


def _says(text):
    """A model that answers in one round with no tool calls."""
    def stream(resolved, messages, tools=None):
        yield {"type": "content", "text": text}
    return stream


class TestRunningThemAtOnce:
    def test_each_investigation_comes_back_under_its_own_heading(self, monkeypatch):
        monkeypatch.setattr(subagents.router_mod, "stream_events", _says("what I found"))
        out = subagents.explore(
            [{"name": "Database and Config", "task": "how is config loaded"},
             {"name": "API and Middleware", "task": "what are the routes"}],
            run_tool=lambda n, a: "", tools=[], emit=lambda e: None)
        assert "### Database and Config" in out
        assert "### API and Middleware" in out
        assert out.count("what I found") == 2

    def test_they_come_back_in_the_order_they_were_asked_for(self, monkeypatch):
        """Not the order they finish in. The agent wrote the list, and reading
        it back shuffled is a small tax on every answer that follows."""
        def slow_first(resolved, messages, tools=None):
            if "first" in messages[-1]["content"]:
                time.sleep(0.4)
            yield {"type": "content", "text": "done"}
        monkeypatch.setattr(subagents.router_mod, "stream_events", slow_first)
        out = subagents.explore([{"name": "One", "task": "the first thing"},
                                 {"name": "Two", "task": "the second thing"}],
                                run_tool=lambda n, a: "", tools=[], emit=lambda e: None)
        assert out.index("### One") < out.index("### Two")

    def test_they_really_do_run_at_the_same_time(self, monkeypatch):
        """Sequentially this is the thing it was built to replace."""
        live, peak = [], [0]
        lock = threading.Lock()

        def counting(resolved, messages, tools=None):
            with lock:
                live.append(1)
                peak[0] = max(peak[0], len(live))
            time.sleep(0.3)
            with lock:
                live.pop()
            yield {"type": "content", "text": "done"}

        monkeypatch.setattr(subagents.router_mod, "stream_events", counting)
        subagents.explore([{"name": f"A{n}", "task": f"question {n}"} for n in range(3)],
                          run_tool=lambda n, a: "", tools=[], emit=lambda e: None)
        assert peak[0] == 3

    def test_one_failing_does_not_take_the_others_down(self, monkeypatch):
        """The whole point of splitting the question up is that the parts are
        independent."""
        def flaky(resolved, messages, tools=None):
            if "explodes" in messages[-1]["content"]:
                raise RuntimeError("provider said no")
            yield {"type": "content", "text": "fine"}

        monkeypatch.setattr(subagents.router_mod, "stream_events", flaky)
        out = subagents.explore([{"name": "Bad", "task": "the one that explodes"},
                                 {"name": "Good", "task": "the ordinary one"}],
                                run_tool=lambda n, a: "", tools=[], emit=lambda e: None)
        assert "provider said no" in out
        assert "fine" in out

    def test_the_panel_hears_about_each_one_starting_and_finishing(self, monkeypatch):
        """Four agents working is four cards ticking, not one long pause and
        then a wall of text."""
        monkeypatch.setattr(subagents.router_mod, "stream_events", _says("x"))
        events = []
        subagents.explore([{"name": "One", "task": "a question"}],
                          run_tool=lambda n, a: "", tools=[], emit=events.append)
        states = [e["subagent"]["state"] for e in events if "subagent" in e]
        assert states == ["running", "done"]


class TestWhatTheyCannotDo:
    def test_the_tool_list_is_a_whitelist_of_reads(self):
        """A subtraction would mean a mutating tool added anywhere else in the
        app quietly becomes something four parallel agents can call at once."""
        for name in subagents.SUBAGENT_TOOLS:
            assert name in agent_tools.TOOLS, name
            assert agent_tools.TOOLS[name]["mutating"] is False, name

    def test_nothing_that_writes_is_in_it(self):
        for forbidden in ("write_file", "edit_file", "run_command", "start_server",
                          "delete_file", "save_skill", "git_commit"):
            assert forbidden not in subagents.SUBAGENT_TOOLS

    def test_a_call_to_a_tool_they_were_not_given_is_refused(self, monkeypatch, isolated_db):
        """A tool list is a suggestion — a model will call a name it saw in
        training and was never offered, and the one place that must not be
        true is where four agents are running unattended."""
        seen = []

        def writes_a_file(resolved, messages, tools=None):
            if len(messages) < 3:
                yield {"type": "tool_calls", "calls": [
                    {"id": "1", "function": {"name": "carrot__write_file",
                                             "arguments": {"path": "x", "content": "y"}}}]}
            else:
                yield {"type": "content", "text": "gave up"}

        monkeypatch.setattr(subagents.router_mod, "stream_events", writes_a_file)
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: ".")
        real_write = agent_tools.TOOLS["write_file"]["handler"]
        monkeypatch.setitem(agent_tools.TOOLS["write_file"], "handler",
                            lambda **kw: seen.append(kw) or "wrote it")

        agent_tools._tool_explore_in_parallel(
            investigations=[{"name": "Sneaky", "task": "look around"}], emit=lambda e: None)
        assert seen == [], "a subagent reached a write tool"
        assert real_write is not None

    def test_they_cannot_spawn_more_of_themselves(self):
        """Recursion here is not a runaway loop, it is a runaway fan-out."""
        assert "explore_in_parallel" not in subagents.SUBAGENT_TOOLS

    def test_the_fan_out_is_capped(self, monkeypatch):
        monkeypatch.setattr(subagents.router_mod, "stream_events", _says("x"))
        out = subagents.explore([{"name": f"A{n}", "task": f"q{n}"} for n in range(12)],
                                run_tool=lambda n, a: "", tools=[], emit=lambda e: None)
        assert out.count("###") == subagents.MAX_SUBAGENTS

    def test_a_subagent_gets_few_rounds(self):
        """A question needing eight rounds should have gone to the main agent
        whole, not to an investigation that reports a paragraph."""
        assert subagents.MAX_ROUNDS <= 5


class TestWhenItIsOfferedAtAll:
    def test_it_is_off_for_a_local_model_by_default(self, monkeypatch, isolated_db):
        """Four conversations against one 8B on one GPU is not four times the
        work — it is the same work, serialised, with queueing on top."""
        class Local:
            provider, model, local = "ollama", "llama3.2:8b", True
        monkeypatch.setattr(subagents.router_mod, "route", lambda *a, **k: Local())
        assert subagents.enabled() is False
        out = agent_tools._tool_explore_in_parallel(
            investigations=[{"task": "anything"}])
        assert "switched off" in out

    def test_a_user_with_the_hardware_can_say_otherwise(self, monkeypatch, isolated_db):
        class Local:
            provider, model, local = "ollama", "llama3.2:8b", True
        monkeypatch.setattr(subagents.router_mod, "route", lambda *a, **k: Local())
        monkeypatch.setattr(subagents, "get_config", lambda: {"subagents_enabled": True})
        assert subagents.enabled() is True

    def test_it_is_on_for_a_hosted_model(self, isolated_db, monkeypatch):
        monkeypatch.setattr(subagents, "get_config", lambda: {})
        assert subagents.enabled() is True

    def test_asking_for_nothing_is_an_error_not_four_empty_agents(self, isolated_db):
        assert "error" in agent_tools._tool_explore_in_parallel(investigations=[])


def test_the_tool_is_not_gated_as_a_write():
    """Everything underneath it is a read, and a confirmation prompt for
    reading is a prompt people turn off."""
    assert agent_tools.TOOLS["explore_in_parallel"]["mutating"] is False
