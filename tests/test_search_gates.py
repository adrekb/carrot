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


def drive(script, mode, question="status of the F-15EX program", synthesis=None,
          tool_result="RESULT"):
    """Run one chat turn against a scripted model.

    `script` is a list of (assistant_text, tool_calls) per round. Once it is
    exhausted the model replies with `synthesis` and no tool calls.

    `tool_result` is what every tool returns. Pass search output containing
    URLs to exercise the path where the server opens the pages itself.
    """
    read_urls = []
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
         patch.object(A, "_run_tool", _runner(tool_result, read_urls)), \
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
        "auto_read": [e["tool"]["args"]["url"] for e in events
                      if "tool" in e and e["tool"].get("auto")],
        "read_urls": read_urls,
    }


def _runner(tool_result, read_urls):
    """Stand-in for the tool runner that records what got opened."""
    def run(name, args, conversation_id):
        if name.endswith("read_url"):
            url = args.get("url", "")
            read_urls.append(url)
            return iter([{"_tool_result": f"PAGE TEXT from {url}"}])
        return iter([{"_tool_result": tool_result}])
    return run


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
        """Nudging forever would mean never answering at all.

        With no URLs in these results there is nothing for the server to open
        on the model's behalf either, so the turn falls through to taking what
        it has — flagged, rather than passed off as researched.
        """
        result = drive([("", tool_call("web_search", query="F-15EX a")),
                        ("Answer.", [])] * 8, A.SEARCH_MULTI)
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
            if "QUESTION:" in messages[-1]["content"]:
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
        assert "QUESTION:" in seen["asked"]
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


class TestGateCountersActuallyCount:
    """The root cause of every "(no response)" report.

    Tools are offered to the model namespaced as `carrot__web_search`, but the
    counters compared against the bare name. So `searches` and `reads` stayed
    at zero no matter what the model did: the gate could never be satisfied,
    every turn burned all three nudges telling a model that had just read six
    pages that it had not searched yet, and every turn ended stalled.
    """

    class Route:
        def as_dict(self):
            return {}

    def run(self, tool_names, mode=A.SEARCH_MULTI):
        """A model that makes the given calls, one per round, then answers."""
        remaining = list(tool_names)

        def fake(resolved, messages, tools=None):
            if remaining:
                name = remaining.pop(0)
                yield {"type": "tool_calls", "calls": [
                    {"id": name, "function": {"name": name, "arguments": {
                        "query": "recent american political news",
                        "url": "https://apnews.com/politics",
                    }}}]}
                return
            yield {"type": "text", "text": "Here is the answer."}

        with patch.object(A.router_mod, "stream_events", fake), \
             patch.object(A, "_run_tool", lambda n, a, c: iter([{"_tool_result": "page text"}])), \
             patch.object(A, "_available_tools", lambda m: [{"name": "x"}]):
            return list(A._agentic_chat_events(
                [{"role": "user", "content": "recent american political news"}],
                self.Route(), mode=mode))

    def test_a_namespaced_search_counts_as_a_search(self):
        events = self.run(["carrot__web_search", "carrot__web_search", "carrot__read_url"])
        # Gates met, so no nudge should ever have been emitted.
        assert not [e for e in events if "gate" in e]

    def test_the_answer_survives_when_the_gates_are_met(self):
        events = self.run(["carrot__web_search", "carrot__web_search", "carrot__read_url"])
        final = next(e["_final_text"] for e in events if "_final_text" in e)
        assert final == "Here is the answer."

    def test_a_genuinely_thin_turn_still_gets_nudged(self):
        # The gate must keep working — this is not a matter of disabling it.
        events = self.run(["carrot__read_url"])
        assert [e for e in events if "gate" in e]

    def test_research_is_not_suggested_after_a_properly_researched_turn(self):
        # Offering to escalate a turn that did the work is noise.
        events = self.run(["carrot__web_search", "carrot__web_search", "carrot__read_url"])
        assert not [e for e in events if "suggest_research" in e]

    def test_query_drift_is_detected_on_a_namespaced_call(self):
        # Same prefix bug: the drift check never fired either.
        def fake(resolved, messages, tools=None):
            if not any(m.get("role") == "tool" for m in messages):
                yield {"type": "tool_calls", "calls": [{
                    "id": "1",
                    "function": {"name": "carrot__web_search",
                                 "arguments": {"query": "banana bread recipe"}},
                }]}
                return
            yield {"type": "text", "text": "done"}

        with patch.object(A.router_mod, "stream_events", fake), \
             patch.object(A, "_run_tool", lambda n, a, c: iter([{"_tool_result": "x"}])), \
             patch.object(A, "_available_tools", lambda m: [{"name": "x"}]):
            events = list(A._agentic_chat_events(
                [{"role": "user", "content": "who won the senate race in Ohio"}],
                self.Route(), mode=A.SEARCH_MULTI))
        assert any(e.get("tool", {}).get("rejected") for e in events if "tool" in e)


class TestAProviderThatStopsTalking:
    """The reported failure, third time round, and the cause I kept missing.

    Every earlier fix for "(no response)" was written *inside* the tool loop.
    None of them ran, because the throw was a provider error escaping the
    generator — and by then FastAPI had already committed a 200 with headers
    sent, so the response could not become an error. The socket simply closed,
    the browser had no text, and it printed "(no response)". These pin the
    guarantee at both layers.
    """

    def exploding(self, after=1, message="context length exceeded"):
        """A model that answers `after` rounds and then fails like an API."""
        state = {"rounds": 0}

        def fake_stream(resolved, messages, tools=None):
            state["rounds"] += 1
            if state["rounds"] > after:
                raise RuntimeError(message)
            yield {"type": "tool_calls",
                   "calls": tool_call("web_search", query="F-15EX program status")}
        return fake_stream

    def run(self, stream_fn, mode=A.SEARCH_MULTI):
        class Route:
            def as_dict(self):
                return {}

        with patch.object(A.router_mod, "stream_events", stream_fn), \
             patch.object(A, "_run_tool",
                          lambda n, a, c: iter([{"_tool_result": "PAGE TEXT"}])), \
             patch.object(A, "_available_tools", lambda m: []):
            history = [{"role": "user", "content": "status of the F-15EX program"}]
            return list(A._agentic_chat_events(history, Route(), None, None, mode))

    def test_a_provider_error_still_produces_an_answer(self):
        events = self.run(self.exploding())
        final = next(e["_final_text"] for e in events if "_final_text" in e)
        assert final.strip(), "the turn ended with nothing to show the user"

    def test_the_provider_error_is_named_not_swallowed(self):
        # "the model ran out of room" and "your key is rate limited" need
        # completely different things from the user. Guessing between them is
        # what made this unfixable for three rounds.
        events = self.run(self.exploding(message="429 rate limit"))
        assert any("provider_error" in e for e in events)
        final = next(e["_final_text"] for e in events if "_final_text" in e)
        assert "429 rate limit" in final

    def test_what_the_turn_gathered_is_not_thrown_away(self):
        events = self.run(self.exploding())
        final = next(e["_final_text"] for e in events if "_final_text" in e)
        assert "PAGE TEXT" in final or "F-15EX" in final

    def test_a_failure_on_the_very_first_round_still_answers(self):
        events = self.run(self.exploding(after=0))
        final = next(e["_final_text"] for e in events if "_final_text" in e)
        assert final.strip()

    def test_single_turn_mode_is_covered_too(self):
        events = self.run(self.exploding(), mode=A.SEARCH_SINGLE)
        final = next(e["_final_text"] for e in events if "_final_text" in e)
        assert final.strip()


class TestDeepeningIsNotDrift:
    """The check that rejected four correct searches in a row.

    "What is happening in American politics" leads to "August 4 2026 primary
    winners Kansas Missouri" — no shared word, and the first version of the
    drift check called that a change of subject. It was the whole point of
    multi-turn search.
    """

    def test_a_narrowing_follow_up_is_allowed(self):
        result = drive([
            ("", tool_call("web_search", query="American political news August 2026")),
            ("", tool_call("read_url", url="https://apnews.com/x")),
            ("", tool_call("web_search",
                           query="August 4 2026 primary winners Kansas Missouri")),
            ("Here is what happened.", []),
        ], A.SEARCH_MULTI, question="what is happening in American politics")
        assert result["rejected"] == [], \
            "a legitimate deepening search was refused as off-topic"
        assert result["ran"].count("web_search") == 2

    def test_a_real_change_of_subject_is_still_caught(self):
        result = drive([
            ("", tool_call("web_search", query="sourdough starter hydration ratio")),
            ("", tool_call("web_search", query="F-15EX unit cost")),
            ("", tool_call("read_url", url="http://example.com")),
            ("Answer.", []),
        ], A.SEARCH_MULTI, question="status of the F-15EX program")
        assert "sourdough starter hydration ratio" in result["rejected"]

    def test_refusals_cannot_consume_the_whole_round_budget(self):
        # Four rejected searches and no answer is the reported trace. A refusal
        # the model does not act on is a wasted round, so the check stands down.
        script = [("", tool_call("web_search", query=f"unrelated topic {i}"))
                  for i in range(8)]
        result = drive(script, A.SEARCH_MULTI, question="status of the F-15EX program")
        assert len(result["rejected"]) <= A.MAX_QUERY_REJECTIONS
        assert result["final"].strip()


class TestTheStreamItself:
    """The outermost guarantee: the SSE body cannot end in silence.

    By the time the body generator runs, the 200 and the headers are already
    on the wire. An exception there is not an error response — it is a closed
    socket, which is indistinguishable from a finished turn with no text.
    """

    def test_a_crash_in_the_turn_still_streams_text_and_done(self, client):
        def explode(*args, **kwargs):
            raise RuntimeError("something broke deep inside")

        with patch.object(A, "_agentic_chat_events", explode):
            response = client.post("/api/chat/stream",
                                   json={"message": "hello", "stream": True})
        body = response.text
        assert response.status_code == 200
        assert "something broke deep inside" in body
        assert '"done": true' in body.lower()

    def test_failing_to_save_the_turn_does_not_lose_the_answer(self, client):
        def one_chunk(*args, **kwargs):
            yield {"chunk": "the answer"}
            yield {"_final_text": "the answer"}

        with patch.object(A, "_agentic_chat_events", one_chunk), \
             patch.object(A, "_post_turn", side_effect=RuntimeError("disk full")):
            response = client.post("/api/chat/stream",
                                   json={"message": "hello", "stream": True})
        assert "the answer" in response.text
        assert '"done": true' in response.text.lower()


# ===== When the model will not open a page, the server opens it =====

SEARCH_WITH_URLS = (
    "- F-15EX deliveries hit 12 — https://www.defensenews.com/f15ex-12 [Defense News]\n"
    "- Boeing F-15EX programme page — https://boeing.com/f15ex [Boeing]\n"
    "- Air Force budget request — https://af.mil/budget-2027 [Af.Mil]\n"
)


class TestTheServerReadsWhenTheModelWillNot:
    """The gate assumed the model *can* open a page when told to.

    From a reported turn: it searched once, was told three times that snippets
    are not an answer, never called read_url, and fell back to writing from the
    result list — the exact failure the gate exists to prevent. Telling it a
    fourth time was never going to work. Reading a page is something the server
    can just do.
    """

    def refuses_to_read(self, rounds=8):
        return [("", tool_call("web_search", query="F-15EX status")),
                ("Here are some outlets.", [])] * rounds

    def test_the_pages_are_opened_for_it(self):
        result = drive(self.refuses_to_read(), A.SEARCH_MULTI,
                       tool_result=SEARCH_WITH_URLS)
        assert result["auto_read"] == ["https://www.defensenews.com/f15ex-12",
                                       "https://boeing.com/f15ex"]

    def test_it_stops_at_the_limit(self):
        result = drive(self.refuses_to_read(), A.SEARCH_MULTI,
                       tool_result=SEARCH_WITH_URLS)
        assert len(result["auto_read"]) == A.AUTO_READ_LIMIT

    def test_it_happens_once_and_not_every_round(self):
        # If page text still did not produce an answer, two more pages are not
        # the missing piece — they are just a slower way to fail.
        result = drive(self.refuses_to_read(rounds=12), A.SEARCH_MULTI,
                       tool_result=SEARCH_WITH_URLS)
        assert len(result["read_urls"]) <= A.AUTO_READ_LIMIT

    def test_one_nudge_comes_first(self):
        # Asking is cheaper than fetching, and plenty of models comply.
        result = drive(self.refuses_to_read(), A.SEARCH_MULTI,
                       tool_result=SEARCH_WITH_URLS)
        assert result["gates"][:1] == [A.GATE_NUDGE_NO_READ]

    def test_a_model_that_reads_on_its_own_is_left_alone(self):
        result = drive([
            ("", tool_call("web_search", query="F-15EX status")),
            ("", tool_call("read_url", url="https://www.defensenews.com/f15ex-12")),
            ("", tool_call("web_search", query="F-15EX deliveries 2026")),
            ("Twelve delivered.", []),
        ], A.SEARCH_MULTI, tool_result=SEARCH_WITH_URLS)
        assert result["auto_read"] == []
        assert result["final"] == "Twelve delivered."

    def test_single_mode_never_does_it(self):
        # Single promises one pass. Fetching pages behind the model's back
        # would make it something else.
        result = drive([
            ("", tool_call("web_search", query="F-15EX status")),
            ("From the titles.", []),
        ], A.SEARCH_SINGLE, tool_result=SEARCH_WITH_URLS)
        assert result["auto_read"] == []

    def test_nothing_openable_falls_through_rather_than_looping(self):
        # Every result blocked, or none at all: the model is not at fault and
        # another nudge would only spend a round.
        result = drive(self.refuses_to_read(), A.SEARCH_MULTI,
                       tool_result="no results (the search backend may be unreachable)")
        assert result["auto_read"] == []
        assert result["final"] == "Here are some outlets."


class TestPickingWhatToOpen:
    def test_results_come_back_in_rank_order(self):
        evidence = [{"tool": "web_search", "source": "q", "text": SEARCH_WITH_URLS}]
        assert A._unread_result_urls(evidence, set())[:2] == [
            "https://www.defensenews.com/f15ex-12", "https://boeing.com/f15ex"]

    def test_a_page_already_opened_is_not_opened_again(self):
        evidence = [{"tool": "web_search", "source": "q", "text": SEARCH_WITH_URLS}]
        urls = A._unread_result_urls(evidence, {"https://www.defensenews.com/f15ex-12"})
        assert "https://www.defensenews.com/f15ex-12" not in urls

    def test_duplicates_across_searches_appear_once(self):
        evidence = [{"tool": "web_search", "source": "a", "text": SEARCH_WITH_URLS},
                    {"tool": "web_search", "source": "b", "text": SEARCH_WITH_URLS}]
        urls = A._unread_result_urls(evidence, set())
        assert len(urls) == len(set(urls))

    def test_pages_the_model_read_are_not_candidates(self):
        # Only search results are offered. A page already fetched is evidence,
        # not a lead.
        evidence = [{"tool": "read_url", "source": "https://x", "text": "https://y"}]
        assert A._unread_result_urls(evidence, set()) == []

    def test_trailing_punctuation_is_not_part_of_the_url(self):
        evidence = [{"tool": "web_search", "source": "q",
                     "text": "see https://example.com/story), and more"}]
        assert A._unread_result_urls(evidence, set()) == ["https://example.com/story"]
