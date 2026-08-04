"""Multi-turn search has to actually be multi-turn.

The mode used to be a request, not a guarantee: the directive told the model
"do not stop at the first set of results", and a small on-device model read
that, searched once, answered from the snippets, and stopped. The user saw a
mode called "Multi-turn search" do a single turn.

These tests drive the tool loop with a scripted model, so they pin the
behaviour the *code* enforces rather than what a particular model happens to
feel like doing.
"""
from unittest.mock import patch

import pytest

from carrot import app as A


def tool_call(name, **args):
    return [{"id": name, "function": {"name": name, "arguments": args}}]


def drive(script, mode, question="status of the F-15EX program", synthesis=None):
    """Run one chat turn against a scripted model.

    `script` is a list of (assistant_text, tool_calls) per round. Once it is
    exhausted the model replies with `synthesis` and no tool calls.
    """
    remaining = iter(script)

    def fake_stream(resolved, messages, tools=None):
        try:
            content, calls = next(remaining)
        except StopIteration:
            content, calls = (synthesis or "(final answer)", [])
        if content:
            yield {"type": "text", "text": content}
        if calls:
            yield {"type": "tool_calls", "calls": calls}

    class Route:
        def as_dict(self):
            return {}

    with patch.object(A.router_mod, "stream_events", fake_stream), \
         patch.object(A, "_run_tool", lambda n, a, c: iter([{"_tool_result": "RESULT"}])), \
         patch.object(A, "_available_tools", lambda m: []):
        history = [{"role": "user", "content": question}]
        events = list(A._agentic_chat_events(history, Route(), None, None, mode))

    return {
        "events": events,
        "final": next(e["_final_text"] for e in events if "_final_text" in e),
        "ran": [e["tool"]["name"] for e in events
                if "tool" in e and not e["tool"].get("rejected")],
        "rejected": [e["tool"]["args"].get("query") for e in events
                     if "tool" in e and e["tool"].get("rejected")],
        "gates": [e["gate"]["reason"] for e in events if "gate" in e],
        "research_offered": any("suggest_research" in e for e in events),
        "streamed": "".join(e["chunk"] for e in events if "chunk" in e),
    }


class TestTheGates:
    def test_answering_from_snippets_is_pushed_back(self):
        """The exact reported failure: one search, then a list of outlets."""
        result = drive([
            ("", tool_call("web_search", query="F-15EX program status")),
            ("Here are some news outlets.", []),          # premature
            ("", tool_call("read_url", url="http://example.com/f15ex")),
            ("", tool_call("web_search", query="F-15EX deliveries 2026")),
            ("The F-15EX has delivered N aircraft.", []),
        ], A.SEARCH_MULTI)
        assert result["gates"], "the model was allowed to answer without reading anything"
        assert result["ran"] == ["web_search", "read_url", "web_search"]
        assert result["final"] == "The F-15EX has delivered N aircraft."

    def test_the_discarded_answer_never_reaches_the_user(self):
        """A premature answer we intend to replace must not be streamed first,
        or the user watches an answer appear and then get swapped out."""
        result = drive([
            ("", tool_call("web_search", query="F-15EX status")),
            ("Here are some news outlets.", []),
            ("", tool_call("read_url", url="http://example.com")),
            ("", tool_call("web_search", query="F-15EX rate")),
            ("Real answer.", []),
        ], A.SEARCH_MULTI)
        assert "news outlets" not in result["streamed"]
        assert result["streamed"] == "Real answer."

    def test_reading_a_page_is_required(self):
        assert A._search_gate_gap(searches=3, reads=0) == A.GATE_NUDGE_NO_READ

    def test_a_second_search_is_required(self):
        assert A._search_gate_gap(searches=1, reads=1) == A.GATE_NUDGE_ONE_SEARCH

    def test_gates_open_once_both_are_met(self):
        assert A._search_gate_gap(searches=2, reads=1) is None

    def test_a_model_that_cannot_comply_still_answers(self):
        """Nudging forever would mean never answering at all. After the cap we
        take what we have — and flag it rather than passing it off."""
        result = drive([("", tool_call("web_search", query="F-15EX a")),
                        ("Answer.", [])] * 8, A.SEARCH_MULTI)
        assert len(result["gates"]) == A.MAX_GATE_NUDGES
        assert result["final"] == "Answer."
        assert result["research_offered"]

    def test_single_mode_is_not_gated(self):
        """Single-pass promises one pass; gating it would change what it means."""
        result = drive([
            ("", tool_call("web_search", query="F-15EX status")),
            ("Short answer.", []),
        ], A.SEARCH_SINGLE)
        assert result["gates"] == []
        assert result["final"] == "Short answer."

    def test_single_mode_still_streams_live(self):
        result = drive([("Answer without searching.", [])], A.SEARCH_SINGLE)
        assert result["streamed"] == "Answer without searching."


class TestQueryDrift:
    """A question about the F-15EX came back with a search for 'current
    American political news'. A query sharing no word with the question is a
    different question, and its results cannot answer the one that was asked."""

    def test_off_topic_query_is_refused_before_it_runs(self):
        result = drive([
            ("", tool_call("web_search", query="current American political news")),
            ("", tool_call("web_search", query="F-15EX program status")),
            ("", tool_call("read_url", url="http://example.com")),
            ("", tool_call("web_search", query="F-15EX deliveries")),
            ("Answer.", []),
        ], A.SEARCH_MULTI)
        assert result["rejected"] == ["current American political news"]
        assert "current American political news" not in result["ran"]

    def test_a_rejected_search_does_not_count_toward_the_gate(self):
        """Otherwise refusing a bad query would help satisfy the requirement
        it was refused for."""
        result = drive([
            ("", tool_call("web_search", query="unrelated trivia nonsense")),
            ("Answer now.", []),
        ], A.SEARCH_MULTI)
        assert result["gates"], "a refused search should not open the gate"

    def test_a_rephrasing_is_allowed(self):
        assert not A._query_drifted("status of the F-15EX program",
                                    "F-15EX Eagle II delivery schedule")

    def test_a_narrower_query_is_allowed(self):
        assert not A._query_drifted("how is the F-15EX program doing",
                                    "F-15EX 2026 production rate Boeing")

    def test_an_unrelated_query_is_drift(self):
        assert A._query_drifted("status of the F-15EX program",
                                "current American political news")

    def test_an_empty_question_never_blocks(self):
        assert not A._query_drifted("", "anything at all")


class TestBudgetExhaustion:
    """Every round going to a tool call used to mean the model was never asked
    to write anything, so the user waited out the whole loop and got an empty
    message."""

    def test_the_model_is_asked_for_an_answer(self):
        seen = {}

        # The synthesis call is identified by the message the loop appends,
        # not by tools being None: the loop passes `tools or None`, so an
        # empty tool list looks identical to the synthesis call.
        def fake_stream(resolved, messages, tools=None):
            if "Stop searching and answer now" in messages[-1]["content"]:
                seen["asked"] = messages[-1]["content"]
                seen["tools"] = tools
                yield {"type": "text", "text": "Partial answer; could not find X."}
                return
            yield {"type": "tool_calls",
                   "calls": tool_call("web_search", query="F-15EX status")}

        class Route:
            def as_dict(self):
                return {}

        with patch.object(A.router_mod, "stream_events", fake_stream), \
             patch.object(A, "_run_tool", lambda n, a, c: iter([{"_tool_result": "R"}])), \
             patch.object(A, "_available_tools", lambda m: [{"name": "web_search"}]):
            events = list(A._agentic_chat_events(
                [{"role": "user", "content": "status of the F-15EX program"}],
                Route(), None, None, A.SEARCH_MULTI))

        final = next(e["_final_text"] for e in events if "_final_text" in e)
        assert final == "Partial answer; could not find X."
        assert "Stop searching and answer now" in seen["asked"]
        assert seen["tools"] is None, "the synthesis call must not offer more tools"
        assert any("suggest_research" in e for e in events)

    def test_the_budget_is_actually_bounded(self):
        result = drive([("", tool_call("web_search", query="F-15EX x"))]
                       * (A.MAX_TOOL_ROUNDS_MULTI + 5), A.SEARCH_MULTI)
        assert len(result["ran"]) == A.MAX_TOOL_ROUNDS_MULTI

    def test_multi_turn_has_the_larger_budget(self):
        assert A.MAX_TOOL_ROUNDS_MULTI > A.MAX_TOOL_ROUNDS


class TestIntermediateChatter:
    def test_thinking_aloud_is_not_glued_onto_the_answer(self):
        """Content from every round used to be concatenated, so "Let me look
        that up." ended up saved as part of the final message."""
        result = drive([
            ("Let me look that up. ", tool_call("web_search", query="F-15EX status")),
            ("", tool_call("read_url", url="http://example.com")),
            ("", tool_call("web_search", query="F-15EX rate")),
            ("The answer is X.", []),
        ], A.SEARCH_MULTI)
        assert result["final"] == "The answer is X."
        assert "Let me look that up" not in result["final"]


class TestResearchHandoff:
    """Escalation is offered, never taken automatically — a chat turn silently
    becoming a multi-minute research run is its own kind of broken."""

    def test_offered_when_the_turn_was_thin(self):
        result = drive([("", tool_call("web_search", query="F-15EX a")),
                        ("Answer.", [])] * 8, A.SEARCH_MULTI)
        assert result["research_offered"]

    def test_not_offered_when_the_turn_did_its_job(self):
        result = drive([
            ("", tool_call("web_search", query="F-15EX program status")),
            ("", tool_call("read_url", url="http://example.com")),
            ("", tool_call("web_search", query="F-15EX deliveries 2026")),
            ("A properly researched answer.", []),
        ], A.SEARCH_MULTI)
        assert not result["research_offered"]

    def test_the_offer_carries_the_question(self):
        result = drive([("", tool_call("web_search", query="F-15EX a")),
                        ("Answer.", [])] * 8, A.SEARCH_MULTI,
                       question="status of the F-15EX program")
        offer = next(e["suggest_research"] for e in result["events"]
                     if "suggest_research" in e)
        assert offer["question"] == "status of the F-15EX program"

    def test_never_offered_outside_multi_turn(self):
        result = drive([("Answer.", [])], A.SEARCH_SINGLE)
        assert not result["research_offered"]
