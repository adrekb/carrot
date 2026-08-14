"""A turn that runs until the window fills, rather than to a round count.

Eight rounds was the wrong unit and it was the one doing the stopping: asked
for the status of an aircraft programme, a turn made fourteen tool calls, hit
the ceiling, and wrote up four bullets. Nothing had gone wrong — it had run
out of permission to keep working, in the middle of working.

Rounds are not what runs out. The context window is, and unlike a round count
it is measurable before the request is sent, because the transcript, the tool
schemas and the directive are all strings we are about to serialise.
"""
import json
from unittest.mock import patch

import pytest

from carrot import app as A, context_windows as ctxwin


class Route:
    provider, model, local = "anthropic", "claude-opus-5", False

    def as_dict(self):
        return {}


def _drive(rounds_of_tools, window, transcript_per_round=""):
    """Run a turn whose model always calls a tool, against a fixed window."""
    calls = {"n": 0}

    def fake_stream(resolved, messages, tools=None):
        calls["n"] += 1
        if calls["n"] > rounds_of_tools:
            yield {"type": "content", "text": "the answer"}
            return
        yield {"type": "tool_calls", "calls": [
            {"id": str(calls["n"]), "function": {"name": "carrot__web_search",
                                                 "arguments": {"query": "x"}}}]}

    result = transcript_per_round or "R"
    with patch.object(A.router_mod, "stream_events", fake_stream), \
         patch.object(A, "_run_tool", lambda n, a, c: iter([{"_tool_result": result}])), \
         patch.object(A, "_available_tools", lambda m: [{"name": "web_search"}]), \
         patch.object(A, "_window_tokens", lambda resolved: window):
        return list(A._agentic_chat_events(
            [{"role": "user", "content": "status of the F-15EX programme"}],
            Route(), None, None, A.SEARCH_MULTI))


class TestTheLoopRunsOnRoom:
    def test_a_turn_may_pass_the_old_round_ceiling(self):
        """The complaint this came from. Fourteen calls into a question that
        needed them, the turn stopped and answered in four bullets."""
        events = _drive(A.MAX_TOOL_ROUNDS_MULTI + 3, window=200_000)
        rounds = [e for e in events if "context" in e]
        assert len(rounds) > A.MAX_TOOL_ROUNDS_MULTI

    def test_it_stops_when_the_window_is_nearly_full(self):
        """Before it is actually full: the estimate is four-characters-per-
        token, the provider counts differently, and a turn that overruns gets
        a hard error instead of a written answer."""
        events = _drive(50, window=4000, transcript_per_round="x" * 4000)
        stopped = [e for e in events
                   if e.get("stage") == "context" and "full" in (e.get("detail") or "")]
        assert stopped, "the turn never noticed it was out of room"

    def test_running_out_of_room_is_said_rather_than_implied(self):
        """'It ran out of room' and 'it decided it was done' produce the same
        short answer and call for opposite things from the user — a bigger
        window versus a better question."""
        events = _drive(50, window=4000, transcript_per_round="x" * 4000)
        detail = next(e["detail"] for e in events if e.get("stage") == "context")
        assert "%" in detail and "context window" in detail

    def test_the_ceiling_is_still_there_for_a_loop_that_gathers_nothing(self):
        """A model calling list_dir on the same directory adds almost nothing
        each time, so the window never notices. That is a bug, not deep work,
        and it should not cost fifty calls to a metered provider first."""
        events = _drive(A.MAX_TOOL_ROUNDS_CEILING + 20, window=1_000_000)
        rounds = [e for e in events if "context" in e]
        assert len(rounds) == A.MAX_TOOL_ROUNDS_CEILING

    def test_an_unknown_window_turns_the_check_off_rather_than_guessing(self):
        """Zero is the honest answer for a custom endpoint nobody has told us
        about. Inventing a ceiling would stop turns at a number with nothing
        behind it."""
        events = _drive(3, window=0)
        assert not [e for e in events if "context" in e]
        assert any("_final_text" in e for e in events)


class TestTheMeter:
    def test_every_round_reports_what_it_will_cost(self):
        """The bar is most useful while there is still room to act on it. One
        that appears only once the turn is doomed is an epitaph."""
        events = [e["context"] for e in _drive(3, window=100_000) if "context" in e]
        assert len(events) >= 3
        assert all(0 <= e["fraction"] <= 1 for e in events)
        assert all(e["window"] == 100_000 for e in events)

    def test_the_reading_grows_as_the_transcript_does(self):
        events = [e["context"] for e in _drive(4, window=100_000, transcript_per_round="y" * 2000)
                  if "context" in e]
        assert events[-1]["used"] > events[0]["used"]

    def test_the_tool_schemas_are_counted_not_just_the_conversation(self):
        """They are in the window before the first word of the question, and
        on a multi-turn search they are a third of an 8k one."""
        source = (__import__("pathlib").Path(A.__file__)).read_text(encoding="utf-8")
        assert "tools_tokens = ctxwin_mod.estimate_tokens" in source

    def test_the_panel_draws_it(self):
        from pathlib import Path

        features = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
                    / "features.js").read_text(encoding="utf-8")
        css = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "css"
               / "style.css").read_text(encoding="utf-8")
        assert "payload.context" in features
        for cls in (".ctx-meter", ".ctx-bar", ".ctx-text", ".ctx-meter.full"):
            assert cls in css, cls

    def test_it_stays_quiet_while_the_window_is_mostly_empty(self):
        """A meter at 3% is decoration, and a panel that decorates every turn
        is one people stop reading."""
        from pathlib import Path

        features = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
                    / "features.js").read_text(encoding="utf-8")
        assert "CONTEXT_METER_FROM" in features
