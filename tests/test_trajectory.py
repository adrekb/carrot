"""A run, as a shape rather than as a transcript.

The trace says what happened; it does not say where the run went. "How many
times did it read a file", "which turn took the minute", "did it search before
or after it edited" are the three questions anybody has about a session they
did not watch, and all three are answered today by scrolling the whole thing
and holding it in your head.

Nothing new is recorded. Every assistant row already carries its trace and its
metrics, so this is an assembler over what exists — which is also why it cannot
disagree with the transcript beside it.
"""
import json
import re
from pathlib import Path

import pytest

from carrot import trajectory

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"


def conv(*messages):
    return {"messages": list(messages)}


def ask(text="do the thing", at="2026-08-18T10:00:00Z"):
    return {"role": "user", "content": text, "timestamp": at}


def answered(text="done", trace=None, **metrics):
    meta = {"trace": trace or []}
    meta.update(metrics)
    return {"role": "assistant", "content": text, "metadata": json.dumps(meta)}


def tool(name, args=None, result="ok", rejected=False):
    return [{"tool": {"name": name, "args": args or {}, "rejected": rejected}},
            {"tool_result": {"result": result}}]


class TestATurnIsAQuestionAndWhatAnsweredIt:
    def test_a_question_opens_a_turn(self):
        out = trajectory.for_conversation(conv(ask("first"), answered()))
        assert out["totals"]["turns"] == 1
        assert out["turns"][0]["question"] == "first"

    def test_turns_are_numbered_from_one(self):
        out = trajectory.for_conversation(
            conv(ask("a"), answered(), ask("b"), answered()))
        assert [t["index"] for t in out["turns"]] == [1, 2]

    def test_an_unanswered_question_is_still_a_turn(self):
        """A run that was stopped, or is still going, is exactly the one you
        want to look at."""
        out = trajectory.for_conversation(conv(ask("a"), answered(), ask("b")))
        assert out["totals"]["turns"] == 2
        assert out["turns"][1]["steps"] == []

    def test_an_answer_with_no_question_gets_its_own_turn(self):
        """A recap, a scheduled run, anything the app started on its own. It
        did happen, so dropping it would make the count a lie."""
        out = trajectory.for_conversation(conv(answered("morning brief")))
        assert out["totals"]["turns"] == 1
        assert out["turns"][0]["question"] == ""

    def test_an_empty_conversation_is_empty(self):
        assert trajectory.for_conversation(conv())["turns"] == []
        assert trajectory.for_conversation(None)["turns"] == []


class TestWhatEachTurnDid:
    def test_tools_are_counted(self):
        out = trajectory.for_conversation(conv(ask(), answered(
            trace=[*tool("carrot__read_file"), *tool("carrot__edit_file")])))
        assert out["turns"][0]["tools"] == 2
        assert out["totals"]["tools"] == 2

    def test_a_refused_tool_is_shown_and_not_counted(self):
        """Plan mode refuses writes. The call was made and is worth seeing;
        it did not happen and must not appear in "19 tool calls"."""
        out = trajectory.for_conversation(conv(ask(), answered(
            trace=[{"tool": {"name": "carrot__write_file", "args": {}, "rejected": True}}])))
        assert out["turns"][0]["tools"] == 0
        step = out["turns"][0]["steps"][0]
        assert step["kind"] == "tool" and step["rejected"] is True

    def test_the_namespace_is_stripped(self):
        """`carrot__read_file` is plumbing; `read_file` is the tool."""
        out = trajectory.for_conversation(conv(ask(), answered(trace=tool("carrot__read_file"))))
        assert out["turns"][0]["steps"][0]["name"] == "read_file"

    def test_a_result_is_paired_with_its_call(self):
        out = trajectory.for_conversation(conv(ask(), answered(
            trace=[*tool("carrot__read_file", result="import requests")])))
        step = out["turns"][0]["steps"][0]
        assert step["result"] == "import requests"
        assert step["ok"] is True

    @pytest.mark.parametrize("result,ok", [
        ("[ok]\nfine", True),
        ("[exit 0]\nfine", True),
        ("[exit 1]\nboom", False),
        ("error: no such file", False),
        ("[failed] nope", False),
    ])
    def test_failure_is_read_the_same_way_the_cards_read_it(self, result, ok):
        """So a run reads the same in the trajectory and in the transcript.
        `[exit 0]` is a success — a test that treats every `[exit N]` as a
        failure marks passing commands red."""
        out = trajectory.for_conversation(conv(ask(), answered(trace=tool("t", result=result))))
        assert out["turns"][0]["steps"][0]["ok"] is ok

    def test_the_route_is_a_step(self):
        out = trajectory.for_conversation(conv(ask(), answered(
            trace=[{"route": {"provider": "ollama", "model": "phi4:14b", "local": True}}])))
        step = out["turns"][0]["steps"][0]
        assert step == {"kind": "route", "label": "ollama/phi4:14b", "local": True}

    def test_thinking_is_a_size_not_a_transcript(self):
        """The trace view beside this is where you read it. Here the question
        is whether it thought and roughly how much."""
        out = trajectory.for_conversation(conv(ask(), answered(trace=[{"thinking": "x" * 500}])))
        step = out["turns"][0]["steps"][0]
        assert step["kind"] == "thinking" and step["chars"] == 500

    def test_a_plan_reports_its_progress(self):
        out = trajectory.for_conversation(conv(ask(), answered(
            trace=[{"plan": {"goals": ["a", "b", "c"], "done": ["a"]}}])))
        step = out["turns"][0]["steps"][0]
        assert (step["kind"], step["goals"], step["done"]) == ("plan", 3, 1)

    def test_an_interrupted_turn_says_so(self):
        out = trajectory.for_conversation(conv(ask(), answered(interrupted=True)))
        assert out["turns"][0]["steps"][-1]["kind"] == "stopped"

    def test_a_provider_error_is_a_step(self):
        out = trajectory.for_conversation(conv(ask(), answered(
            trace=[{"provider_error": {"message": "500 Server Error"}}])))
        step = out["turns"][0]["steps"][0]
        assert step["kind"] == "error" and "500" in step["detail"]

    def test_the_steps_keep_their_order(self):
        """Half the value is *when* a thing happened — did it search before or
        after it edited."""
        out = trajectory.for_conversation(conv(ask(), answered(trace=[
            {"route": {"provider": "ollama", "model": "m"}},
            *tool("carrot__web_search"),
            *tool("carrot__edit_file"),
        ])))
        kinds = [s["kind"] for s in out["turns"][0]["steps"]]
        assert kinds == ["route", "tool", "tool", "answer"]
        names = [s["name"] for s in out["turns"][0]["steps"] if s["kind"] == "tool"]
        assert names == ["web_search", "edit_file"]


class TestWhatItCost:
    def test_metrics_come_off_the_row(self):
        out = trajectory.for_conversation(conv(ask(), answered(
            seconds=41.2, tokens=1719, model="phi4:14b")))
        turn = out["turns"][0]
        assert (turn["seconds"], turn["tokens"], turn["model"]) == (41.2, 1719, "phi4:14b")

    def test_totals_add_up(self):
        out = trajectory.for_conversation(conv(
            ask(), answered(seconds=10, tokens=100),
            ask(), answered(seconds=5.5, tokens=50)))
        assert out["totals"]["seconds"] == 15.5
        assert out["totals"]["tokens"] == 150

    def test_a_turn_the_model_did_not_time_reports_nothing(self):
        """`_turn_metrics` is empty for a hosted model or a reply built
        entirely from tool output. Timing it from the row timestamps instead
        would measure the gap between two database writes, which includes
        however long the browser was closed."""
        out = trajectory.for_conversation(conv(ask(), answered()))
        assert out["turns"][0]["seconds"] is None

    def test_nothing_measured_is_not_a_total_of_zero(self):
        """Zero is a measurement and this is its absence — a run of hosted
        turns would otherwise claim to have taken no time at all."""
        out = trajectory.for_conversation(conv(ask(), answered()))
        assert out["totals"]["seconds"] is None

    def test_a_partly_timed_run_totals_what_it_has(self):
        out = trajectory.for_conversation(conv(
            ask(), answered(seconds=12), ask(), answered()))
        assert out["totals"]["seconds"] == 12


class TestItSurvivesWhatIsOnDisk:
    def test_metadata_may_be_a_dict_or_a_string(self):
        """`get_conversation` parses it; other paths hand it over raw."""
        raw = {"role": "assistant", "content": "x",
               "metadata": {"trace": tool("carrot__read_file"), "seconds": 3}}
        out = trajectory.for_conversation(conv(ask(), raw))
        assert out["turns"][0]["tools"] == 1
        assert out["turns"][0]["seconds"] == 3

    def test_unparseable_metadata_costs_that_turn_its_detail_only(self):
        broken = {"role": "assistant", "content": "x", "metadata": "{{{"}
        out = trajectory.for_conversation(conv(ask(), broken))
        assert out["totals"]["turns"] == 1
        assert out["turns"][0]["steps"][0]["kind"] == "answer"

    def test_a_junk_trace_entry_is_skipped(self):
        out = trajectory.for_conversation(conv(ask(), answered(
            trace=["not a dict", {"tool": "not a dict either"}, *tool("carrot__ok")])))
        assert out["turns"][0]["tools"] == 1

    def test_long_values_are_clipped(self):
        out = trajectory.for_conversation(conv(ask("q" * 900), answered(
            trace=tool("t", args={"path": "p" * 900}, result="r" * 900))))
        turn = out["turns"][0]
        assert len(turn["question"]) <= 220
        assert len(turn["steps"][0]["result"]) <= trajectory.MAX_RESULT_CHARS + 2
        assert len(turn["steps"][0]["args"]) <= trajectory.MAX_ARGS_CHARS + 2


class TestOpeningAStep:
    """The row is scanned; the expansion is read. A tool row says which tool
    and whether it failed — the thing you go looking for next is what it was
    given and what came back."""

    def test_a_tool_carries_its_full_call_and_result(self):
        out = trajectory.for_conversation(conv(ask(), answered(
            trace=tool("t", args={"command": "pytest -q"},
                       result="[exit 1]\nline one\nline two"))))
        step = out["turns"][0]["steps"][0]
        assert step["args_full"] == "command=pytest -q"
        assert step["result_full"] == "[exit 1]\nline one\nline two"

    def test_the_expansion_keeps_the_lines(self):
        """A command's output is its lines. The row flattens them because it is
        one line; flattening them in the expansion too would leave nowhere to
        read it."""
        out = trajectory.for_conversation(conv(ask(), answered(
            trace=tool("t", result="one\ntwo"))))
        step = out["turns"][0]["steps"][0]
        assert "\n" in step["result_full"]
        assert "\n" not in step["result"]

    def test_a_plan_says_which_goals_not_just_how_many(self):
        """"2/3" tells you it did not finish. This tells you what it did not
        finish."""
        out = trajectory.for_conversation(conv(ask(), answered(
            trace=[{"plan": {"goals": ["find it", "add backoff", "run tests"],
                             "done": ["find it"]}}])))
        detail = out["turns"][0]["steps"][0]["detail"]
        assert "[x] find it" in detail
        assert "[ ] run tests" in detail

    def test_thinking_is_capped_for_the_panel(self):
        """A turn may hold 12,000 characters of it, and a wall of reasoning in
        the middle of a trajectory hides the four tool calls underneath. The
        transcript is where you read it whole."""
        out = trajectory.for_conversation(conv(ask(), answered(trace=[{"thinking": "x" * 9000}])))
        step = out["turns"][0]["steps"][0]
        assert step["chars"] == 9000
        assert len(step["detail"]) <= trajectory.MAX_THINKING_CHARS + 2

    def test_a_step_with_nothing_more_has_no_detail(self):
        """A `<details>` that opens onto nothing is the worst of the three
        states a disclosure can be in."""
        out = trajectory.for_conversation(conv(ask(), answered(
            trace=[{"route": {"provider": "ollama", "model": "m"}}])))
        route = out["turns"][0]["steps"][0]
        assert "detail" not in route

    def test_the_panel_only_makes_a_disclosure_when_there_is_one(self):
        features = (WEB / "js" / "features.js").read_text(encoding="utf-8")
        body = re.search(r"function trajStep\(step\)\s*\{(.*?)\n\}", features, re.DOTALL).group(1)
        assert "if (!full) return" in body

    def test_the_expansion_wraps_rather_than_scrolling_sideways(self):
        """A horizontal scrollbar inside a panel you opened to read something
        is a second thing to operate before you can read it."""
        css = (WEB / "css" / "style.css").read_text(encoding="utf-8")
        rule = re.search(r"\.traj-full \{([^}]*)\}", css).group(1)
        assert "white-space: pre-wrap" in rule


class TestBothSurfacesShowIt:
    """An agent run in chat is the same shape of thing as one in the Code tab —
    turns, tools, time — and "which turn took the minute" does not become a
    different question because of which tab it was started in."""

    def test_chat_has_a_control_and_a_panel(self):
        index = (WEB / "index.html").read_text(encoding="utf-8")
        assert 'onclick="toggleChatTrajectory()"' in index
        assert 'id="chat-trajectory"' in index

    def test_the_code_tab_still_has_its_own(self):
        index = (WEB / "index.html").read_text(encoding="utf-8")
        assert 'onclick="toggleTrajectory()"' in index
        assert 'id="agent-trajectory"' in index

    def test_they_share_one_renderer(self):
        """Two views of one run that are drawn by two functions are two views
        that will drift."""
        features = (WEB / "js" / "features.js").read_text(encoding="utf-8")
        assert features.count("function renderTrajectory(") == 1
        assert "toggleTrajectoryIn(" in features

    def test_each_reads_its_own_conversation(self):
        """The Code tab's session and the chat's are different conversations,
        and a shared panel that forgets which is which shows the wrong run."""
        features = (WEB / "js" / "features.js").read_text(encoding="utf-8")
        assert "() => agentConversationId" in features
        assert "() => currentConversationId" in features

    def test_it_reads_the_id_when_it_loads_not_when_it_opens(self):
        """Captured as a value it goes stale the moment a run starts after the
        panel was opened — which is the normal case, because you open a
        trajectory to watch a run rather than after one."""
        features = (WEB / "js" / "features.js").read_text(encoding="utf-8")
        assert "trajectoryConversationOf = conversationOf;" in features
        assert "const conversationId = trajectoryConversationOf();" in features

    def test_toggling_the_other_surface_does_not_close_it(self):
        """`trajectoryOpen` is one flag for two panels: opening the second while
        the first is open must not read as a close."""
        features = (WEB / "js" / "features.js").read_text(encoding="utf-8")
        assert "trajectoryOpen = !(trajectoryOpen && trajectoryHostId === hostId);" in features


class TestTheEndpoint:
    def test_it_returns_the_trajectory(self, client):
        from carrot import conversation as conv_mod

        created = conv_mod.create_conversation(title="run")
        conv_mod.add_message(created["id"], "user", "do it")
        conv_mod.add_message(created["id"], "assistant", "done",
                             metadata={"trace": tool("carrot__read_file"), "seconds": 2})
        payload = client.get(f"/api/conversations/{created['id']}/trajectory").json()
        assert payload["totals"] == {"turns": 1, "tools": 1, "seconds": 2, "tokens": None}

    def test_a_missing_conversation_is_a_404(self, client):
        assert client.get("/api/conversations/nope/trajectory").status_code == 404


class TestThePanel:
    def test_there_is_a_way_to_open_it(self):
        index = (WEB / "index.html").read_text(encoding="utf-8")
        assert 'onclick="toggleTrajectory()"' in index
        assert 'id="agent-trajectory"' in index

    def test_it_replaces_the_log_rather_than_crowding_it(self):
        """Two readings of one run; showing both at once means neither has the
        width."""
        features = (WEB / "js" / "features.js").read_text(encoding="utf-8")
        body = re.search(r"async function toggleTrajectory\(\)\s*\{(.*?)\n\}",
                         features, re.DOTALL).group(1)
        assert "log.classList.toggle('hidden', trajectoryOpen)" in features

    def test_the_bars_are_scaled_to_the_run(self):
        """Against a fixed maximum every bar in a fast session is a stub, and
        the question the bars answer — which turn was slow — is relative."""
        features = (WEB / "js" / "features.js").read_text(encoding="utf-8")
        body = re.search(r"function renderTrajectory\(data\)\s*\{(.*?)\n\}",
                         features, re.DOTALL).group(1)
        assert "Math.max(...turns.map(t => t.seconds || 0)" in body

    def test_duration_is_readable_over_a_minute(self):
        """"184s" is a number you have to convert before it means anything."""
        features = (WEB / "js" / "features.js").read_text(encoding="utf-8")
        body = re.search(r"function trajDuration\(seconds\)\s*\{(.*?)\n\}",
                         features, re.DOTALL).group(1)
        assert "60" in body and "padStart" in body

    def test_every_step_kind_has_a_mark(self):
        """A kind with no glyph renders as a bare dot, which is the one row you
        cannot identify while scanning the column."""
        features = (WEB / "js" / "features.js").read_text(encoding="utf-8")
        marks = re.search(r"TRAJECTORY_MARKS = \{(.*?)\n\};", features, re.DOTALL).group(1)
        for kind in ("route", "thinking", "plan", "tool", "answer", "error", "stopped"):
            assert f"{kind}:" in marks, kind
