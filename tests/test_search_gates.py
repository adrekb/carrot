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
          tool_result="RESULT", coder=False):
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
        events = list(A._agentic_chat_events(history, Route(), None, None, mode,
                                             coder=coder))

    return {
        "events": events,
        "plans": [e["plan"] for e in events if "plan" in e],
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
        """Still bounded, at a much higher number than it used to be.

        A round count was the wrong unit and it was the one doing the
        stopping: a turn two calls from the answer hit eight and wrote up
        whatever it had. What runs out is the context window, which is
        measurable, so that is what stops the loop now — and this ceiling is
        the backstop for the case the window cannot catch, a model calling
        list_dir on the same directory forever and adding nothing each time.
        """
        result = drive([("", tool_call("web_search", query="F-15EX x"))]
                       * (A.MAX_TOOL_ROUNDS_CEILING + 5), A.SEARCH_MULTI)
        assert len(result["ran"]) == A.MAX_TOOL_ROUNDS_CEILING

    def test_a_turn_may_now_run_past_the_old_ceiling(self):
        """The complaint this came from: fourteen tool calls into a question
        that needed them, the turn stopped and answered in four bullets."""
        result = drive([("", tool_call("web_search", query="F-15EX x"))]
                       * (A.MAX_TOOL_ROUNDS_MULTI + 4), A.SEARCH_MULTI)
        assert len(result["ran"]) > A.MAX_TOOL_ROUNDS_MULTI

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


# ===== The plan, and refusing to stop short of it =====

def drive_planned(script, plan, mode=None, question="c8 zr1x specs",
                  tool_result="- ZR1X page - https://gmauthority.com/zr1x [Gm]"):
    """Drive a turn with a scripted plan as well as a scripted model."""
    from unittest.mock import patch

    with patch.object(A, "_research_plan", lambda resolved, q: plan):
        return drive(script, mode or A.SEARCH_MULTI, question=question,
                     tool_result=tool_result)


class TestACoverageReportIsNotAnAnswer:
    """Three reports, three shapes, one failure.

        "Specs available include: 0-60 time, quarter mile times, top speed"
        "The following resources cover technical specifications for the ZR1X"
        "The provided notes do not contain specific performance specifications.
         I would need technical data sheets or a full article."

    The last is the clearest: rounds left, knew exactly what was missing, said
    so, and stopped. Each reads as diligence, which is why they kept shipping.
    """

    @pytest.mark.parametrize("answer", [
        "The provided notes do not contain specific performance specifications for the ZR1X.",
        "I would need technical data sheets or a full article containing engine figures.",
        "Specs available include: 0-60 time, quarter mile times, top speed, price.",
        "The following resources cover technical specifications for the C8 ZR1X.",
        "These sources cover the vehicle performance and pricing.",
        "The search results do not contain the horsepower figure.",
        "For the latest figures, check the manufacturer website.",
    ])
    def test_the_shapes_that_kept_shipping(self, answer):
        assert A._reads_like_a_coverage_report(answer) is True

    @pytest.mark.parametrize("answer", [
        "The ZR1X makes 1,250 hp and reaches 60 mph in 1.89 seconds.",
        "It uses a twin-turbo 5.5-litre V8 with an electric front axle. I could not "
        "find a confirmed kerb weight.",
        "Prices start at $187,495 according to Car and Driver.",
        "",
    ])
    def test_a_real_answer_is_left_alone(self, answer):
        # "I could not find X" inside an answer that gives the other figures is
        # a useful admission, not the failure shape. What is caught is the case
        # where describing the material has *replaced* answering.
        assert A._reads_like_a_coverage_report(answer) is False

    def test_the_turn_is_sent_back_for_it(self):
        result = drive_planned([
            ("", tool_call("web_search", query="c8 zr1x specs")),
            ("", tool_call("read_url", url="https://gmauthority.com/zr1x")),
            ("", tool_call("web_search", query="c8 zr1x engine")),
            ("The provided notes do not contain specific specifications.", []),
            ("It makes 1,250 hp.", []),
        ], plan=[])
        assert any("report on your own reading" in g for g in result["gates"])
        assert result["final"] == "It makes 1,250 hp."

    def test_it_gives_up_rather_than_looping(self):
        # A model that only ever produces coverage reports must still finish.
        result = drive_planned([
            ("", tool_call("web_search", query="a")),
            ("", tool_call("read_url", url="https://gmauthority.com/zr1x")),
            ("", tool_call("web_search", query="b")),
            ("The notes do not contain that.", []),
        ] * 8, plan=[])
        assert result["final"]
        coverage = [g for g in result["gates"] if "report on your own reading" in g]
        assert len(coverage) <= A.MAX_GOAL_NUDGES


class TestTheTurnWorksToAPlan:
    def test_the_plan_is_stated_before_searching(self):
        result = drive_planned([
            ("", tool_call("web_search", query="c8 zr1x")),
            ("", tool_call("read_url", url="https://gmauthority.com/zr1x")),
            ("", tool_call("web_search", query="zr1x engine")),
            ("It makes 1250 horsepower with a twin-turbo engine and costs $187,495.", []),
        ], plan=["What engine does it use?", "What does it cost?"])
        plans = [e["plan"]["goals"] for e in result["events"] if "plan" in e]
        assert plans and plans[0] == ["What engine does it use?", "What does it cost?"]

    def test_an_answer_touching_every_goal_is_accepted(self):
        result = drive_planned([
            ("", tool_call("web_search", query="c8 zr1x")),
            ("", tool_call("read_url", url="https://gmauthority.com/zr1x")),
            ("", tool_call("web_search", query="zr1x price")),
            ("It uses a twin-turbo engine and costs $187,495.", []),
        ], plan=["What engine does it use?", "What does it cost?"])
        assert result["final"] == "It uses a twin-turbo engine and costs $187,495."
        assert not any(e.get("gate", {}).get("unmet")
                       for e in result["events"] if "gate" in e)

    def test_an_untouched_goal_sends_it_back(self):
        result = drive_planned([
            ("", tool_call("web_search", query="c8 zr1x")),
            ("", tool_call("read_url", url="https://gmauthority.com/zr1x")),
            ("", tool_call("web_search", query="zr1x engine")),
            ("It uses a twin-turbo engine.", []),          # nothing about price
            ("It uses a twin-turbo engine and costs $187,495.", []),
        ], plan=["What engine does it use?", "What does it cost?"])
        unmet = [e["gate"]["unmet"] for e in result["events"]
                 if "gate" in e and e["gate"].get("unmet")]
        assert unmet and "What does it cost?" in unmet[0]
        assert result["final"] == "It uses a twin-turbo engine and costs $187,495."

    def test_it_stops_arguing_about_coverage_eventually(self):
        result = drive_planned([
            ("", tool_call("web_search", query="a")),
            ("", tool_call("read_url", url="https://gmauthority.com/zr1x")),
            ("", tool_call("web_search", query="b")),
            ("It uses a twin-turbo engine.", []),
        ] * 8, plan=["What engine does it use?", "What does it cost?"])
        assert result["final"] == "It uses a twin-turbo engine."
        goal_gates = [e for e in result["events"]
                      if "gate" in e and e["gate"].get("unmet")]
        assert len(goal_gates) <= A.MAX_GOAL_NUDGES

    def test_single_pass_gets_no_plan(self):
        # The plan costs a model call before any searching. Single-pass exists
        # to be quick, and one round cannot be checked against a plan anyway.
        result = drive_planned([
            ("", tool_call("web_search", query="c8 zr1x")),
            ("Short answer.", []),
        ], plan=["What engine does it use?"], mode=A.SEARCH_SINGLE)
        assert not any("plan" in e for e in result["events"])


class TestReadingThePlan:
    def test_it_takes_questions_and_drops_narration(self):
        from unittest.mock import patch

        raw = ("Here is my plan:\n"
               "1. What is a C8 ZR1X?\n"
               "- What engine does it use?\n"
               "I will then summarise.\n"
               "* How much does it cost?\n")
        with patch.object(A.router_mod, "complete", lambda r, m: raw):
            goals = A._research_plan(object(), "c8 zr1x specs")
        assert goals == ["What is a C8 ZR1X?", "What engine does it use?",
                         "How much does it cost?"]

    def test_it_is_capped(self):
        from unittest.mock import patch

        raw = "\n".join(f"Question number {n}?" for n in range(20))
        with patch.object(A.router_mod, "complete", lambda r, m: raw):
            assert len(A._research_plan(object(), "q")) == A.MAX_GOALS

    def test_a_model_that_cannot_plan_does_not_break_the_turn(self):
        # Planning that failed closed would make the mode fragile for exactly
        # the models that need it most.
        from unittest.mock import patch

        def boom(resolved, messages):
            raise RuntimeError("no")

        with patch.object(A.router_mod, "complete", boom):
            assert A._research_plan(object(), "q") == []

    def test_the_prompt_says_to_identify_an_unknown_thing_first(self):
        # "c8 zr1X" became "Toyota C-HR ZR1X" because it replaced a term it did
        # not know with one it did. The plan is where that gets caught.
        assert "your FIRST question must be what that thing actually is" in A.PLAN_PROMPT


class TestUnmetGoals:
    def test_words_from_the_question_do_not_count(self):
        # They turn up in any answer, so matching on them would mark every goal
        # as touched and the check would never fire.
        assert A._unmet_goals(["What is the kerb weight?"], "c8 zr1x specs",
                              "Here are the zr1x specs.") == ["What is the kerb weight?"]

    def test_a_goal_made_only_of_the_questions_words_is_treated_as_met(self):
        # It cannot be checked — every answer contains those words — and the
        # alternative is nudging forever over a goal no evidence could satisfy.
        assert A._unmet_goals(["What are the specs of the zr1x?"],
                              "c8 zr1x specs", "Here are the zr1x specs.") == []

    def test_a_plural_still_counts(self):
        # The goal says "what does it cost", the answer says "costs $187,495".
        # Without the lightest stemming the turn is sent back to find something
        # it had already found.
        assert A._unmet_goals(["What does it cost?"], "c8 zr1x specs",
                              "It costs $187,495.") == []

    def test_one_matching_term_is_enough(self):
        # The job is catching a silently dropped goal, not grading coverage. A
        # check that argued about quality would nudge forever.
        assert A._unmet_goals(["What engine does it use?"], "c8 zr1x specs",
                              "It uses a twin-turbo engine.") == []

    def test_no_goals_means_nothing_to_be_unmet(self):
        assert A._unmet_goals([], "q", "anything") == []


# ===== The coding agent works to a plan too =====
#
# ACT mode's failure is the same shape as multi-turn's, one layer over: it stops
# because it has *done* something, not because it has done the thing. Asked for
# four files it writes one and describes the other three, and the description
# reads exactly like the work. So the steps are checked against the tool calls,
# never against the prose — in ACT mode prose is where the shortfall hides.

def drive_coder(script, plan, act=True, question="add a --json flag to cli.py",
                tool_result="ok"):
    """Drive a coding turn with a scripted plan, as the Code tab would."""
    from unittest.mock import patch

    with patch.object(A, "_coder_plan", lambda resolved, q: plan), \
         patch.object(A, "_act_mode_now", lambda: act):
        return drive(script, A.SEARCH_SINGLE, question=question,
                     tool_result=tool_result, coder=True)


class TestTheCodingPlan:
    def test_the_steps_are_stated_before_any_work(self):
        result = drive_coder([
            ("", tool_call("edit_file", path="cli.py", content="--json flag")),
            ("Added the flag and its tests.", []),
        ], plan=["Add a --json flag to cli.py", "Cover it in test_cli.py"])
        assert result["plans"], "the coding turn never published a plan"
        assert result["plans"][0] == {
            "goals": ["Add a --json flag to cli.py", "Cover it in test_cli.py"],
            "done": [],
        }

    def test_a_step_ticks_when_a_tool_touches_it(self):
        result = drive_coder([
            ("", tool_call("edit_file", path="cli.py", content="add --json flag")),
            ("", tool_call("write_file", path="test_cli.py", content="cover the flag")),
            ("Both done.", []),
        ], plan=["Add a --json flag to cli.py", "Cover it in test_cli.py"])
        assert result["plans"][-1]["done"] == [
            "Add a --json flag to cli.py", "Cover it in test_cli.py"]

    def test_describing_a_step_does_not_tick_it(self):
        """The whole reason the plan is checked against `work` and not the answer.

        One file written, the other three described in prose that reads just
        like having done them. If the answer counted, this turn would finish
        with a full checklist and one change on disk.
        """
        result = drive_coder([
            ("", tool_call("edit_file", path="cli.py", content="add --json flag")),
            ("Added the flag to cli.py, and covered it in test_cli.py.", []),
        ], plan=["Add a --json flag to cli.py", "Cover it in test_cli.py"])
        done = result["plans"][-1]["done"]
        assert done == ["Add a --json flag to cli.py"]
        assert "Cover it in test_cli.py" not in done

    def test_an_untouched_step_sends_the_turn_back(self):
        result = drive_coder([
            ("", tool_call("edit_file", path="cli.py", content="add --json flag")),
            ("Added the flag to cli.py, and covered it in test_cli.py.", []),
            ("", tool_call("write_file", path="test_cli.py", content="cover the flag")),
            ("Both done, for real this time.", []),
        ], plan=["Add a --json flag to cli.py", "Cover it in test_cli.py"])
        assert result["gates"], "it stopped with a step nobody had run"
        assert "Cover it in test_cli.py" in result["gates"][0]
        assert result["final"] == "Both done, for real this time."

    def test_the_nudge_is_capped(self):
        """A model that will not use the tools still gets to finish.

        Nudging forever is the failure the search gates already learned: the
        turn has to end with whatever was managed, not spin to the budget.
        """
        result = drive_coder([
            ("", tool_call("edit_file", path="cli.py", content="add --json flag")),
            ("I have also covered it in test_cli.py.", []),
        ] * 8, plan=["Add a --json flag to cli.py", "Cover it in test_cli.py"])
        assert len(result["gates"]) <= A.MAX_GOAL_NUDGES

    def test_plan_mode_gets_no_checklist(self):
        """PLAN mode's whole output is a plan the user reads and approves.

        A second machine-made checklist above it is noise, and with the write
        tools withheld there is nothing for one to tick against anyway.
        """
        result = drive_coder([
            ("Here is what I would change, and why.", []),
        ], plan=["Add a --json flag to cli.py"], act=False)
        assert not result["plans"]
        assert not result["gates"]

    def test_an_ordinary_chat_turn_gets_no_coding_plan(self):
        """The plan costs a model call before any work, and `coder` is the only
        thing that says this turn came from the Code tab."""
        from unittest.mock import patch
        with patch.object(A, "_coder_plan", lambda resolved, q: ["never asked"]), \
             patch.object(A, "_act_mode_now", lambda: True):
            result = drive([("Sure.", [])], A.SEARCH_SINGLE)
        assert not result["plans"]


class TestDraftingTheCodingPlan:
    def test_steps_are_taken_and_decoration_dropped(self):
        raw = ("Implementation plan:\n"
               "1. Add a --json flag to cli.py\n"
               "- Update the parser in args.py\n"
               "* Run the test suite\n"
               "\n"
               "Let me know if this works.\n")
        with patch.object(A.router_mod, "complete", lambda r, m: raw):
            steps = A._coder_plan(object(), "add a --json flag")
        assert steps == ["Add a --json flag to cli.py",
                         "Update the parser in args.py",
                         "Run the test suite"]

    def test_a_heading_is_not_a_step(self):
        with patch.object(A.router_mod, "complete",
                          lambda r, m: "Here is the plan:\nEdit cli.py to add --json"):
            assert A._coder_plan(object(), "q") == ["Edit cli.py to add --json"]

    def test_a_question_is_not_a_step(self):
        # Asking is PLAN mode's job, and it has a form for it. A question here
        # would sit on the checklist unanswerable and never tick.
        with patch.object(A.router_mod, "complete",
                          lambda r, m: "Should it be --json or --format?\nEdit cli.py to add --json"):
            assert A._coder_plan(object(), "q") == ["Edit cli.py to add --json"]

    def test_the_plan_is_capped(self):
        raw = "\n".join(f"Edit file{i}.py to add the flag" for i in range(20))
        with patch.object(A.router_mod, "complete", lambda r, m: raw):
            assert len(A._coder_plan(object(), "q")) == A.MAX_GOALS

    def test_a_model_that_cannot_plan_does_not_break_the_turn(self):
        def boom(resolved, messages):
            raise RuntimeError("no")
        with patch.object(A.router_mod, "complete", boom):
            assert A._coder_plan(object(), "q") == []


class TestWhatCountsAsWork:
    def test_paths_and_arguments_carry_the_step_words(self):
        terms = A._work_terms("edit_file", {"path": "cli.py", "content": "--json"}, "ok")
        assert "cli.py" in terms and "--json" in terms

    def test_a_noisy_command_cannot_swamp_the_plan(self):
        """A run_command that dumps a test suite would otherwise mark every
        step done on the strength of one command's output."""
        terms = A._work_terms("run_command", {"cmd": "pytest"}, "x" * 5000)
        assert len(terms) < 1000

    def test_non_string_arguments_are_skipped(self):
        # Tool arguments are whatever the model produced, not a fixed schema.
        A._work_terms("edit_file", {"path": "cli.py", "line": 42, "flags": None}, "ok")

    def test_narration_is_not_a_step(self):
        """A step of questions could be told from narration by the question
        mark; a step of work has no such marker. It matters more than tidiness:
        a line like this shares no words with anything the tools will ever do,
        so it can never tick, and would spend both nudges on non-work."""
        raw = ("Edit cli.py to add --json\n"
               "Let me know if this works.\n"
               "I'll update the docs too.\n")
        with patch.object(A.router_mod, "complete", lambda r, m: raw):
            assert A._coder_plan(object(), "q") == ["Edit cli.py to add --json"]


class TestAnIdentifierSpeltBetterIsStillTheIdentifier:
    """The guard fired on its own subject and made the search worse.

    Asked for "f35 status", the model's first search was "F-35 delivery status
    2025 2026 Lockheed Martin production deliveries TR-3 Block 4" — a better
    query than the question. It was refused for "dropping f35", because `f35`
    and `f-35` are different strings. The model then complied literally and
    searched `f35`, which is the worst query it could have run.

    So the check produced the exact failure it exists to prevent, on the turn
    it was watching. Hyphens, dots and spaces inside a model number are
    typography; the point is catching a substitution, never a spelling.
    """

    @pytest.mark.parametrize("question,query", [
        ("f35 status", "F-35 delivery status 2026 Lockheed Martin production"),
        ("f-15ex specs", "F15EX engine specifications"),
        ("gpt4 pricing", "GPT-4 pricing per token"),
        ("c8 zr1x specs", "C8 ZR1X engine specifications"),
    ])
    def test_a_spelling_variant_is_not_a_dropped_identifier(self, question, query):
        assert A._dropped_identifiers(question, query) == set()

    def test_a_real_substitution_is_still_caught(self):
        # The turn this check was built for: `c8` dropped, a car it had heard
        # of substituted, eleven pages read about a Toyota.
        assert A._dropped_identifiers(
            "c8 zr1x specs", "Toyota C-HR ZR1X 2026 specifications") == {"c8"}

    def test_a_different_model_number_is_still_caught(self):
        assert A._dropped_identifiers("f35 status", "F-22 Raptor status") == {"f35"}

    def test_the_normaliser_only_removes_separators(self):
        assert A._identifier_key("f-35") == "f35"
        assert A._identifier_key("gpt-4o") == "gpt4o"
        assert A._identifier_key("c8") == "c8"
        # It must not collapse two genuinely different identifiers.
        assert A._identifier_key("f35") != A._identifier_key("f22")


class TestTheFirstHalfOfTheAnswerSurvives:
    """Reported: an answer that begins at a bullet, heading and intro gone.

    `content_parts` resets each round, so only the last round's prose became
    the answer. A model that writes its opening, realises it needs one more
    lookup, and then continues had its opening deleted — and because that text
    went into the transcript as something the assistant had already said, the
    model never wrote it again. It believed it had been delivered.
    """

    def test_prose_written_before_a_tool_call_is_kept(self):
        result = drive([
            ("", tool_call("web_search", query="c8 zr1x specs")),
            ("", tool_call("read_url", url="http://example.com/zr1x")),
            ("## C8 ZR1X\n\nThe hybrid flagship Corvette.",
             tool_call("web_search", query="c8 zr1x horsepower")),
            ("* Engine: 5.5L twin-turbo V8\n* Front motor: 186 hp", []),
        ], A.SEARCH_MULTI, question="c8 zr1x specs")
        assert "C8 ZR1X" in result["final"], "the heading was dropped"
        assert "hybrid flagship" in result["final"], "the opening was dropped"
        assert "Front motor" in result["final"], "the closing was dropped"

    def test_narration_is_not_glued_to_the_answer(self):
        """"Let me search for that" is not answer text and belongs nowhere
        near it. Restoring everything indiscriminately would trade one bug for
        a different one."""
        assert A._restore_carried(["Let me search for that."], "1,250 hp.") == "1,250 hp."
        assert A._restore_carried(["I'll look up the figures."], "1,250 hp.") == "1,250 hp."
        assert A._restore_carried(["Okay, searching now."], "1,250 hp.") == "1,250 hp."

    def test_a_restated_opening_is_not_duplicated(self):
        # A model that continues from its own opening usually restates it with
        # small edits, so the check is on a prefix rather than the whole.
        carried = ["The ZR1X is the hybrid flagship Corvette."]
        final = "The ZR1X is the hybrid flagship Corvette. It makes 1,250 hp."
        assert A._restore_carried(carried, final).count("hybrid flagship") == 1

    def test_several_pieces_keep_their_order(self):
        assert A._restore_carried(["## Heading", "First paragraph."], "* bullet") == (
            "## Heading\n\nFirst paragraph.\n\n* bullet")

    def test_a_turn_with_nothing_carried_is_untouched(self):
        assert A._restore_carried([], "Just the answer.") == "Just the answer."


def drive_checked(script, verdict, question="c8 zr1x specs"):
    """Drive a turn with a scripted answer-support checker.

    `router_mod.complete` serves both the plan and the check; returning JSON
    means the plan parser finds no questions in it and the turn runs unplanned,
    which is what isolates this gate.
    """
    from unittest.mock import patch
    with patch.object(A.router_mod, "complete", verdict):
        return drive(script, A.SEARCH_MULTI, question=question)


def support_gates(result):
    return [g for g in result["gates"] if "not in any page" in g]


class TestTheAnswerIsCheckedAgainstThePages:
    """Reported: asked for the C8 ZR1X, the answer said it has "two electric
    motors, one on each front wheel". It has one, and the page it had just
    read said so. Everything around the invention was correct, sourced and
    specific — which is what makes this the worst shape in the app, a
    confident answer with a fabricated detail inside it.

    The search gates force it to look; the goal check forces it to cover the
    question. Neither asks whether what it wrote is what the pages said.
    """

    LIE = "The ZR1X has two electric motors, one on each front wheel. It makes 1,250 hp."
    TRUE = "The ZR1X has one front-axle electric motor of 186 hp. It makes 1,250 hp."

    def searched(self, *answers):
        return [
            ("", tool_call("web_search", query="c8 zr1x specs")),
            ("", tool_call("read_url", url="http://example.com/zr1x")),
            ("", tool_call("web_search", query="c8 zr1x motor")),
        ] + [(a, []) for a in answers]

    def test_a_fabricated_detail_is_sent_back(self):
        result = drive_checked(
            self.searched(self.LIE, self.TRUE),
            lambda r, m: '{"unsupported": ["two electric motors, one on each front wheel"]}')
        assert support_gates(result), "the invention was not caught"
        assert "two electric motors" not in result["final"]

    def test_the_fabrication_never_reaches_the_user(self):
        """Multi-turn withholds prose until the gates are met, so an answer we
        intend to replace must not have been streamed."""
        result = drive_checked(
            self.searched(self.LIE, self.TRUE),
            lambda r, m: '{"unsupported": ["two electric motors, one on each front wheel"]}')
        assert "two electric motors" not in result["streamed"]

    def test_a_clean_answer_is_left_alone(self):
        result = drive_checked(self.searched(self.TRUE, self.TRUE),
                               lambda r, m: '{"unsupported": []}')
        assert support_gates(result) == []
        assert result["final"] == self.TRUE

    def test_a_checker_that_cannot_run_does_not_block_the_answer(self):
        """Failing closed would mean an unavailable check can withhold answers
        over its own availability."""
        def boom(resolved, messages):
            raise RuntimeError("checker offline")
        result = drive_checked(self.searched(self.TRUE, self.TRUE), boom)
        assert result["final"] == self.TRUE
        assert support_gates(result) == []

    def test_a_checker_that_invents_a_quotation_is_ignored(self):
        """It has to quote the answer. A checker that hallucinates is the same
        problem one layer up, and must not be able to send a turn back."""
        result = drive_checked(
            self.searched(self.TRUE, self.TRUE),
            lambda r, m: '{"unsupported": ["a sentence never in the answer at all"]}')
        assert support_gates(result) == []

    def test_junk_from_the_checker_is_ignored(self):
        result = drive_checked(self.searched(self.TRUE, self.TRUE),
                               lambda r, m: "not json at all")
        assert support_gates(result) == []

    def test_it_is_capped(self):
        # It costs a model call and a round; a checker allowed to argue twice
        # about the same paragraph would spend the budget on style.
        result = drive_checked(
            self.searched(*([self.TRUE] * 8)),
            lambda r, m: '{"unsupported": ["' + self.TRUE[:45] + '"]}')
        assert len(support_gates(result)) <= A.MAX_SUPPORT_NUDGES

    def test_every_mode_is_checked(self):
        """Reported as coming back "no matter which mode I try" — correctly,
        because this was multi-turn only. A single-pass turn that reads a page
        can misattribute just as easily; single-pass promises one round of
        searching, not that it will hand over a fact it can see is wrong."""
        from unittest.mock import patch
        with patch.object(A.router_mod, "complete",
                          lambda r, m: '{"unsupported": ["two motors up front"]}'):
            result = drive([("", tool_call("read_url", url="http://example.com/zr1x")),
                            ("The ZR1X has two motors up front.", []),
                            ("The ZR1X has one front motor.", [])],
                           A.SEARCH_SINGLE, question="c8 zr1x specs")
        assert support_gates(result), "single-pass was not checked"
        assert "two motors up front" not in result["final"]

    def test_the_check_asks_who_the_fact_is_about(self):
        """The reported failure was not an invention. Road & Track says three
        times that the ZR1X has one front motor, and then — on the same page —
        that the Lamborghini, Ferrari and Aston "have two motors up front".
        The model lifted the competitor sentence and attached it to the
        Corvette. Every word of it was on the page, which is why it survives a
        check that only asks whether the words appear."""
        assert "Check who each fact is ABOUT" in A.SUPPORT_PROMPT
        assert "rivals" in A.SUPPORT_PROMPT


class TestOneSiteIsNotTheWeb:
    """Asked for "recent us politics news", a turn opened whitehouse.gov five
    times out of six reads and answered entirely from administration press
    releases. Every fact was true and correctly cited, and the answer was
    still wrong: for that question, reading one government press office is a
    press summary, not research — and nothing in the citations tells the
    reader that no second view was ever consulted.

    The search results held Reuters, AP, PBS and Wikipedia. Nothing steered
    towards them and nothing objected when they were skipped.
    """

    def test_a_third_page_from_the_same_site_is_refused(self):
        result = drive([
            ("", tool_call("web_search", query="recent us politics news")),
            ("", tool_call("read_url", url="https://www.whitehouse.gov/a")),
            ("", tool_call("read_url", url="https://www.whitehouse.gov/b")),
            ("", tool_call("read_url", url="https://www.whitehouse.gov/c")),
            ("", tool_call("read_url", url="https://www.reuters.com/x")),
            ("Answer.", []),
        ], A.SEARCH_MULTI, question="recent us politics news")
        assert "https://www.whitehouse.gov/c" not in result["read_urls"]
        assert "https://www.reuters.com/x" in result["read_urls"]

    def test_the_budget_is_not_one_page(self):
        # Two from a site is normal reporting; the failure is five.
        result = drive([
            ("", tool_call("web_search", query="q")),
            ("", tool_call("read_url", url="https://www.reuters.com/a")),
            ("", tool_call("read_url", url="https://www.reuters.com/b")),
            ("Answer.", []),
        ], A.SEARCH_MULTI)
        assert len([u for u in result["read_urls"] if "reuters" in u]) == 2

    def test_www_and_the_bare_domain_are_one_place(self):
        assert A._host_of("https://www.whitehouse.gov/a") == A._host_of("https://whitehouse.gov/b")

    def test_it_is_refused_not_discouraged(self):
        """A rule about balance in the directive is a request. The turn that
        prompted this read one press office five times with the directive in
        front of it the whole way."""
        assert "That page was not opened" in A.HOST_CONCENTRATION_CORRECTION

    def test_this_is_not_corroboration(self):
        """Diversity of reading, not agreement before asserting. Requiring two
        sources to agree would suppress whatever only the primary source says;
        this only asks that somewhere else was looked at first."""
        assert A.MAX_READS_PER_HOST >= 2
