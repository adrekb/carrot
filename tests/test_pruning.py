"""Running out of room should cost the turn its notes, not its work.

Chat's answer to a full context window was to take the tools away and ask for
the best answer available. These tests are about the case that breaks: a turn
that is not gathering to answer but working, and that hits the ceiling holding
everything it needs one step from done.
"""
from unittest.mock import patch

from carrot import app as A, pruning


class Route:
    provider, model, local = "anthropic", "claude-opus-5", False

    def as_dict(self):
        return {}


def drive(rounds_of_tools, window, transcript_per_round="R", question="fix the test"):
    """Run a turn whose model always calls a tool, against a fixed window."""
    calls = {"n": 0}

    def fake_stream(resolved, messages, tools=None):
        calls["n"] += 1
        if calls["n"] > rounds_of_tools or not tools:
            yield {"type": "content", "text": "the answer"}
            return
        yield {"type": "tool_calls", "calls": [
            {"id": str(calls["n"]), "function": {"name": "carrot__read_file",
                                                 "arguments": {"path": "x.py"}}}]}

    with patch.object(A.router_mod, "stream_events", fake_stream), \
         patch.object(A, "_run_tool",
                      lambda n, a, c: iter([{"_tool_result": transcript_per_round}])), \
         patch.object(A, "_available_tools", lambda m: [{"name": "read_file"}]), \
         patch.object(A, "_window_tokens", lambda resolved: window):
        return list(A._agentic_chat_events(
            [{"role": "user", "content": question}],
            Route(), None, None, A.SEARCH_MULTI))


def tool(content, name="read_file", call_id="c1"):
    return {"role": "tool", "content": content, "name": name, "tool_call_id": call_id}


def transcript(results=6, size=4000):
    """A turn that has read several files, oldest first."""
    messages = [{"role": "user", "content": "fix the failing test"}]
    for index in range(results):
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": f"c{index}",
                                         "function": {"name": "read_file"}}]})
        messages.append(tool("x" * size, call_id=f"c{index}"))
    return messages


class TestWhatItTrimsFirst:
    def test_tool_output_goes_before_anything_else(self):
        """It is the only part of a transcript a tool call can rebuild."""
        messages = transcript()
        out, report = pruning.prune(messages, 200)
        assert report["tool_results"] > 0
        assert report["replies"] == 0

    def test_what_the_user_asked_is_never_touched(self):
        """A single enormous file read must not be able to evict the request
        that led to it — which is what one shared budget eventually does."""
        messages = transcript(results=8, size=20000)
        messages.insert(1, {"role": "user", "content": "and " + "please " * 4000})
        out, _ = pruning.prune(messages, 100000)
        users = [m["content"] for m in out if m["role"] == "user"]
        assert users == [m["content"] for m in messages if m["role"] == "user"]

    def test_the_newest_results_survive(self):
        """The most recent result is what the next sentence is about. Trimming
        it is not saving context, it is deleting the thing being worked on."""
        messages = transcript(results=6)
        out, _ = pruning.prune(messages, 100000)
        tools_out = [m for m in out if m["role"] == "tool"]
        kept = tools_out[-pruning.KEEP_RECENT_TOOL_RESULTS:]
        assert all(len(m["content"]) == 4000 for m in kept)

    def test_the_oldest_go_first(self):
        messages = transcript(results=6)
        out, _ = pruning.prune(messages, 300)
        tools_out = [m for m in out if m["role"] == "tool"]
        assert len(tools_out[0]["content"]) < 4000
        assert len(tools_out[-1]["content"]) == 4000

    def test_replies_are_trimmed_only_once_tool_output_runs_out(self):
        messages = [{"role": "user", "content": "go"}]
        for index in range(6):
            messages.append({"role": "assistant", "content": "reasoning " * 500})
            messages.append(tool("x" * 3000, call_id=f"c{index}"))
        out, report = pruning.prune(messages, 100000)
        assert report["tool_results"] > 0 and report["replies"] > 0


class TestWhatItNeverBreaks:
    def test_every_tool_result_keeps_its_envelope(self):
        """A `role: tool` entry answers a tool_call_id in an assistant message
        before it. Drop one and the provider rejects the whole request, so
        content is replaced in place and the message stays."""
        messages = transcript(results=6)
        out, _ = pruning.prune(messages, 100000)
        before = [(m["role"], m.get("tool_call_id")) for m in messages]
        after = [(m["role"], m.get("tool_call_id")) for m in out]
        assert before == after

    def test_a_trimmed_result_still_says_what_it_was(self):
        """A result cut to nothing tells the model only that something was
        there, and a model that cannot see what a search returned runs it
        again — spending a round to rediscover what it just deleted."""
        messages = transcript(results=6)
        out, _ = pruning.prune(messages, 100000)
        first = [m for m in out if m["role"] == "tool"][0]
        assert first["content"].startswith("x" * 100)
        assert "call it again" in first["content"]

    def test_it_does_not_mutate_what_it_was_given(self):
        messages = transcript(results=6)
        pruning.prune(messages, 100000)
        assert all(len(m["content"]) == 4000
                   for m in messages if m["role"] == "tool")

    def test_nothing_to_do_is_not_an_error(self):
        messages = [{"role": "user", "content": "hello"}]
        out, report = pruning.prune(messages, 5000)
        assert out == messages and report["freed"] == 0


class TestKnowingWhetherItIsWorthIt:
    def test_it_can_say_what_it_would_free_without_doing_it(self):
        """The caller has to choose between pruning and giving up honestly
        before it spends anything."""
        messages = transcript(results=6)
        possible = pruning.prunable_tokens(messages)
        _, report = pruning.prune(messages, 10 ** 9)
        assert report["freed"] == possible

    def test_a_transcript_of_one_long_question_has_nothing_to_give(self):
        messages = [{"role": "user", "content": "why " * 20000}]
        assert pruning.prunable_tokens(messages) == 0

    def test_it_stops_once_it_has_what_was_asked_for(self):
        """Pruning past what the turn needs throws away context for nothing."""
        messages = transcript(results=10)
        _, report = pruning.prune(messages, 300)
        assert report["freed"] >= 300
        assert report["tool_results"] < 8


class TestTheTurnLoopUsesIt:
    """Driven through the real loop, with a model that always calls a tool.

    The same harness `test_context_budget` uses, because the question here is
    the one that file answers from the other side: what a turn does at the
    moment the window fills."""

    def test_it_aims_below_the_ceiling_it_just_hit(self):
        from carrot import app
        assert app.CONTEXT_RESUME_FRACTION < app.CONTEXT_STOP_FRACTION

    def test_a_full_window_is_trimmed_before_the_tools_are_taken_away(self):
        events = drive(50, window=4000, transcript_per_round="x" * 4000)
        details = [e["detail"] for e in events if e.get("stage") == "context"]
        trimmed = next(i for i, d in enumerate(details) if "trimmed" in d)
        surrendered = next(i for i, d in enumerate(details) if "answering now" in d)
        assert trimmed < surrendered

    def test_trimming_actually_lowers_the_reading(self):
        """The meter the panel draws has to move, or the turn has spent its
        context telling the user it saved some."""
        events = drive(50, window=4000, transcript_per_round="x" * 4000)
        pruned = next(e["context"] for e in events
                      if "context" in e and e["context"].get("pruned"))
        assert pruned["fraction"] < 0.85

    def test_the_turn_keeps_working_after_being_trimmed(self):
        """The point of all of it. Before, hitting the ceiling ended the
        gathering; now the rounds continue on the other side of it."""
        events = drive(50, window=4000, transcript_per_round="x" * 4000)
        rounds = [e for e in events if "context" in e]
        trimmed_at = next(i for i, e in enumerate(rounds)
                          if e["context"].get("pruned"))
        assert len(rounds) - trimmed_at > 1

    def test_a_turn_with_nothing_to_trim_still_gives_up(self):
        """One enormous question and no tool output: there is nothing pruning
        can take, and saying "I made room" would buy one round and hit the
        same wall — having also deleted what it had."""
        events = drive(50, window=2000, transcript_per_round="x" * 40,
                       question="why " * 4000)
        details = [e["detail"] for e in events if e.get("stage") == "context"]
        assert any("answering now" in d for d in details)
        assert not any("trimmed" in d for d in details)


class TestActLooksBeforeItWrites:
    """The guidance existed in PLAN only, and ACT is a switch the user can
    throw without ever having planned."""

    def test_act_mode_says_to_read_before_overwriting(self):
        from carrot import coder
        preamble = coder.MODE_PREAMBLE[coder.MODE_ACT]
        assert "Look before you write" in preamble
        assert "list_dir" in preamble

    def test_plan_mode_still_says_it_too(self):
        from carrot import coder
        assert "Look before you plan" in coder.MODE_PREAMBLE[coder.MODE_PLAN]
