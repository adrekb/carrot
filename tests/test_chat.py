"""Tests for chat endpoints (streaming and non-streaming) and conversations."""
import asyncio
import json

import pytest

from carrot import app as A


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_chat_non_streaming_creates_conversation(client):
    resp = client.post("/api/chat", json={"message": "Hi Carrot"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["response"] == "Hello from Carrot"
    assert data["conversation_id"]

    # Both the user and assistant messages should be persisted.
    conv = client.get(f"/api/conversations/{data['conversation_id']}").json()
    roles = [m["role"] for m in conv["messages"]]
    assert "user" in roles
    assert "assistant" in roles


def test_chat_stream_returns_sse_chunks(client):
    resp = client.post("/api/chat/stream", json={"message": "Stream please"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    chunks = []
    done = False
    for line in resp.text.split("\n\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = json.loads(line[len("data:"):].strip())
        if payload.get("chunk"):
            chunks.append(payload["chunk"])
        if payload.get("done"):
            done = True

    assert "".join(chunks) == "Hello from Carrot"
    assert done


def test_list_and_create_conversations(client):
    created = client.post("/api/conversations", json={"title": "My chat"}).json()
    assert created["title"] == "My chat"

    convs = client.get("/api/conversations").json()
    assert any(c["id"] == created["id"] for c in convs)


def test_add_message_to_conversation(client):
    conv = client.post("/api/conversations", json={"title": "t"}).json()
    resp = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"role": "user", "content": "hello"},
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "hello"


def test_status_reports_model(client, monkeypatch):
    from carrot import bootstrap as b
    monkeypatch.setattr(b, "get_ollama_executable", lambda: "/usr/bin/ollama")
    monkeypatch.setattr(b, "is_ollama_running", lambda: True)
    monkeypatch.setattr(b, "is_model_available", lambda model=None: True)
    monkeypatch.setattr(
        b, "load_bootstrap_state",
        lambda: {"ollama_installed": True, "model_pulled": True, "model_pulling": False},
    )

    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["default_model"] == "gemma4:e4b"
    assert data["model_loaded"] is True
    assert "bootstrap_complete" in data


class TestStreamingDoesNotBlockTheServer:
    """One chat turn used to starve every other request in the process.

    The SSE bodies were ``async def`` generators whose contents are entirely
    synchronous — blocking HTTP to the provider, and a blocking queue drain in
    ``_run_tool`` while a tool waits on approval. Inside an ``async def`` that
    all runs on the event loop, so for as long as the model was thinking the
    server answered nothing else: /api/health went from 0.01s to hard timeouts
    for the whole turn.

    Approvals could not complete at all, which is why the agent could never
    write a file. Answering a prompt is itself an HTTP call, so the turn could
    not finish until it was answered and it could not be answered until the
    turn finished.

    Starlette only threadpools a body that is *not* already async, and it
    records that by wrapping it in ``iterate_in_threadpool``. Asserting on the
    wrapper is asserting the property — that the blocking work was moved off
    the loop — rather than on the shape of the source.
    """

    def _threadpooled(self, response):
        return getattr(response.body_iterator, "__qualname__", "") == "iterate_in_threadpool"

    def test_the_chat_stream_body_is_run_off_the_event_loop(self):
        from carrot import app as app_mod
        # The generator is lazy: building the response runs none of the body,
        # so no request or conversation has to be faked to inspect it.
        resp = app_mod._chat_stream_response(
            req=None, conv=None, history=None, skill=None, resolved=None)
        assert self._threadpooled(resp), (
            "the chat SSE body is async, so its blocking calls run on the "
            "event loop and stall every other request for the whole turn"
        )

    def test_the_shared_sse_helper_is_run_off_the_event_loop(self):
        from carrot import app as app_mod

        def events():
            yield {"chunk": "x"}

        resp = app_mod._sse(events())
        assert self._threadpooled(resp), (
            "research and agent streams share this helper; async here stalls "
            "the server for the length of a research run"
        )


class TestHowAnswersAreWritten:
    """Two answers to the same question, one rated good and one hard to read,
    differed in shape rather than content. The readable one led each point
    with its claim in bold and explained underneath; ours ran four dense
    paragraphs with the facts buried mid-sentence. Same research, same
    sources, same facts.

    And how much shape someone wants is taste, not correctness — so it is a
    setting with a default rather than a rule.
    """

    KEYS = ("answer_style", "answer_structure", "answer_custom")

    @pytest.fixture(autouse=True)
    def _start_from_the_defaults(self, isolated_db):
        """A fixture, because `setup_method` cannot be ordered after one.

        pytest runs setup_method *before* function-scoped fixtures, so writing
        the config there hit whichever database the previous test left
        configured — after an isolated_db teardown, a temp path that had
        already been removed. `no such table: config`, from a test that never
        touched the table.

        Which test drew the short straw depended on collection order, so the
        suite failed in a different place each run and passed on a rerun,
        which is the shape of flake that gets ignored until it hides something
        real. Taking `isolated_db` as an argument is the whole fix: it now runs
        after the database exists.
        """
        from carrot import config

        for key in self.KEYS:
            config.set_config(key, "")
        yield
        for key in self.KEYS:
            config.set_config(key, "")

    def test_each_style_says_something_different(self, isolated_db):
        from carrot import app, config
        seen = set()
        for style in (app.STYLE_BRIEF, app.STYLE_BALANCED, app.STYLE_FULL):
            config.set_config("answer_style", style)
            seen.add(app.answer_style_directive())
        assert len(seen) == 3

    def test_the_default_is_the_skimmable_one(self, isolated_db):
        from carrot import app
        assert "skimmable" in app.answer_style_directive()

    def test_structure_can_be_turned_down(self, isolated_db):
        from carrot import app, config
        config.set_config("answer_structure", "less")
        assert "flowing prose" in app.answer_style_directive()

    def test_a_custom_instruction_comes_last_and_wins(self, isolated_db):
        """It is the most specific thing the user has said about what they
        want, so it has to be able to override the preset above it."""
        from carrot import app, config
        config.set_config("answer_custom", "never use emoji")
        assert app.answer_style_directive().rstrip().endswith("never use emoji")

    def test_a_custom_instruction_is_bounded(self, isolated_db):
        from carrot import app, config
        config.set_config("answer_custom", "x" * 5000)
        assert len(app.answer_style_directive()) < 3000

    def test_it_reaches_the_turn_before_anything_more_specific(self, isolated_db):
        """A skill's instructions or a document's format should still be able
        to override the house style."""
        from carrot import app
        out = app._prepare_history({"id": "x", "messages": []}, "hi", None,
                                   mode=app.SEARCH_MULTI)
        history = out[0] if isinstance(out, tuple) else out
        assert "skimmable" in history[0]["content"]
        assert any("multi-turn" in m["content"] for m in history if m["role"] == "system")

    def test_a_broken_config_still_answers(self, isolated_db):
        from unittest.mock import patch
        from carrot import app
        with patch.object(app.config, "get_config", side_effect=RuntimeError("no db")):
            assert app.answer_style_directive() == app.ANSWER_STYLES[app.STYLE_DEFAULT]


# ===== Stopping a turn =====
#
# Research and Agent have had a kill switch since they were written. Chat had
# none, so the longest-running thing in the app — a multi-turn search that has
# decided to read six more pages — could only be ended by closing the tab,
# which leaves the provider call running and discards the half-answer.

class TestStoppingAChatTurn:
    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath(*parts).read_text(encoding="utf-8")

    def test_the_turn_announces_its_id_before_it_starts_working(self):
        """The stop button has to exist from the moment there is something to
        stop, and the longest part of a multi-turn run is before the first
        token."""
        app = self.read("carrot", "app.py")
        body = app.split("def _body():")[1].split("def stream():")[0]
        at_turn = body.index("'turn_id': turn_id")
        at_call = body.index("_agentic_chat_events(")
        assert at_turn < at_call

    def test_stopping_an_unknown_turn_is_not_an_error(self, client):
        """Pressing stop as the last token lands is a race the user cannot see."""
        body = client.post("/api/chat/turns/nope/stop")
        assert body.status_code == 200
        assert body.json() == {"stopped": False}

    def test_a_registered_turn_can_be_stopped(self, client):
        from carrot import policy
        context = policy.register_run(policy.RunContext("turn-x"))
        try:
            assert client.post("/api/chat/turns/turn-x/stop").json()["stopped"] is True
            assert context.cancelled is True
        finally:
            policy.release_run("turn-x")

    def test_the_run_is_released_even_if_the_client_disappears(self):
        """A generator closed early never reaches its own last line, and a run
        left in the kernel's table makes `active_runs` lie."""
        app = self.read("carrot", "app.py")
        stream = app.split("def stream():")[1].split("\n    return ")[0]
        assert "finally:" in stream
        assert "release_run(turn_id)" in stream

    def test_chat_does_not_inherit_the_agents_step_ceiling(self):
        """Chat has never had one. Adding it here would be a behaviour change
        smuggled in under a feature — a long multi-turn search that used to
        finish would start dying at 40 steps for reasons nobody asked for."""
        app = self.read("carrot", "app.py")
        block = app.split("stop_context = None")[1].split("def stopped()")[0]
        assert "Budget.from_config" not in block
        assert "10 ** 9" in block

    def test_a_stopped_turn_keeps_what_was_written(self):
        app = self.read("carrot", "app.py")
        assert "final_text = content_str" in app

    def test_a_stopped_turn_is_not_handed_a_manufactured_answer(self):
        """The recovery path is built never to come back empty. Running it over
        a turn the user just stopped is the opposite of what they pressed."""
        app = self.read("carrot", "app.py")
        assert "and not stopped()" in app

    def test_the_browser_says_stopped_rather_than_showing_an_error(self):
        js = self.read("carrot", "web", "js", "app.js")
        assert "AbortError" in js
        assert "stopped-note" in js

    def test_the_server_is_asked_before_the_socket_is_cut(self):
        """Aborting alone leaves the provider call running and billing, and
        throws away the text the clean stop preserves."""
        js = self.read("carrot", "web", "js", "app.js")
        stop = js.split("async function stopChat()")[1].split("\n}")[0]
        assert stop.index("/stop") < stop.index("chatAbort.abort()")
        assert "if (!stopped && chatAbort)" in stop

    def test_send_and_stop_swap_rather_than_sitting_side_by_side(self):
        js = self.read("carrot", "web", "js", "app.js")
        assert "send-btn')?.classList.toggle('hidden', running)" in js
        assert "stop-btn')?.classList.toggle('hidden', !running)" in js


class TestTheGapAnalysisDoesNotReachTheAnswer:
    """The multi-turn loop reflecting on what it still has to find, left at
    the top of the reply.

    Reported from an F-35 turn that opened "From the current results, I still
    cannot answer the following: 1... 2... 3... I will now search for X and Y
    to fill these gaps." and only then began the answer.

    This is not the marker problem — there is no marker, and the tag filter
    correctly refuses to guess at unmarked prose because in general that would
    eventually eat a real answer. This is narrower, which is what makes it
    safe: at the start, ending in an explicit statement of intent to search,
    with a substantial answer after it.
    """

    # Comfortably over the floor below which the strip declines — a real
    # answer is thousands of characters, and a fixture that skirts the limit
    # would be testing the guard rather than the strip.
    BODY = ("The F-35 Lightning II remains in active production and deployment, "
            "but its readiness and modernization efforts face significant "
            "challenges in 2026. The full mission capable rate fell to 25% in "
            "fiscal 2025, down from 38% in 2021. The Pentagon launched a $13.7 "
            "billion sustainment reset to address spare parts shortages and "
            "software issues, but progress has been slow throughout the year. "
            "The TR-3 avionics package remains incomplete, with the Pentagon "
            "accepting a downgraded build that lacks full combat capability, "
            "and no retrofit is planned for the aircraft already delivered "
            "with the earlier hardware configuration. ")

    def test_the_reflection_is_removed(self):
        from carrot import app as A
        text = ("From the current results, I still cannot answer the following:\n"
                "1. The status of Full Operational Capability.\n"
                "2. Engine modernization updates.\n\n"
                "I will now search for F-35 FOC 2026 to fill these gaps.\n" + self.BODY)
        out = A.strip_process_preamble(text)
        assert out.startswith("The F-35 Lightning II")
        assert "still cannot answer" not in out

    def test_several_announcements_are_all_removed(self):
        """A model that lists three gaps and announces two searches should
        lose both announcements, not just the first."""
        from carrot import app as A
        text = ("From the current results, I still cannot answer the following:\n"
                "I will now search for the first thing.\n"
                "Let me also look up the second thing.\n" + self.BODY)
        assert A.strip_process_preamble(text).startswith("The F-35")

    def test_an_ordinary_opening_line_is_kept(self):
        """"Let me check that for you" is a greeting, not a leaked
        reflection — there is no gap list with it."""
        from carrot import app as A
        text = "Let me check that for you.\n" + self.BODY
        assert A.strip_process_preamble(text) == text

    def test_searching_mentioned_mid_answer_is_kept(self):
        """A transition sentence in the middle of a long answer is the model
        narrating mid-flow, and cutting there would delete the answer above
        it."""
        from carrot import app as A
        text = self.BODY + "\nI will now search for the engine details.\n" + self.BODY
        assert A.strip_process_preamble(text) == text

    def test_a_reply_that_is_nothing_but_narration_keeps_every_word(self):
        """Below the floor there is no answer to keep instead, and stripping
        would leave the user with less than the model produced."""
        from carrot import app as A
        text = ("From the current results, I still cannot answer this.\n"
                "I will now search for more.\nA short answer.")
        assert A.strip_process_preamble(text) == text

    def test_it_runs_on_every_finished_answer(self):
        from pathlib import Path
        from carrot import app as A
        source = Path(A.__file__).read_text(encoding="utf-8")
        tidy = source[source.index("def _tidy_answer"):]
        tidy = tidy[:tidy.index("\n\n\n")]
        assert "strip_process_preamble" in tidy


class TestATurnNobodyIsListeningToIsStillWrittenDown:
    """A cancelled run left the question and lost everything else.

    Reported as "it got cancelled and nothing saved, which is not great": the
    conversation kept the user's message and had no assistant row at all — no
    partial answer, no trace, so every search the turn had already run and
    every page it had already read went with it.

    The store used to sit after the event loop. When the browser goes away,
    Starlette closes the generator, which raises GeneratorExit at the `yield`.
    GeneratorExit is a BaseException, so `except Exception` did not catch it
    and nothing after the loop ran.

    Driven by closing the generator directly rather than through TestClient:
    the client buffers the whole response, so a `with` block that exits early
    still consumes every frame and never disconnects. The first version of
    this test passed against the broken code for exactly that reason.
    """

    def _drive(self, monkeypatch, conv_id, events, read=None):
        """Run the endpoint's own generator and, optionally, walk away.

        `read=None` drains it, which is a turn that finished. `read=n` stops
        after n frames and closes, which raises GeneratorExit at the yield —
        exactly what Starlette does when the browser disconnects.
        """
        def fake(*a, **k):
            for event in events:
                yield event
        monkeypatch.setattr(A, "_agentic_chat_events", fake)

        async def go():
            response = await A.chat_stream(A.ChatRequest(
                message="f35 status", conversation_id=conv_id, search_mode="off"))
            frames = response.body_iterator
            seen = 0
            try:
                async for _ in frames:
                    seen += 1
                    if read is not None and seen >= read:
                        break
            finally:
                await frames.aclose()
            return seen
        return asyncio.run(go())

    def test_a_disconnect_mid_stream_still_stores_what_there_was(
            self, client, isolated_db, monkeypatch):
        from carrot import conversation as conv_mod

        conv = conv_mod.create_conversation("cancelled")
        self._drive(monkeypatch, conv["id"], [
            {"tool": {"name": "carrot__web_search", "args": {"query": "f35"}}},
            {"tool_result": {"result": "some results"}},
            {"chunk": "The F-35 is"},
            {"_final_text": "The F-35 is"},
            {"chunk": " a fighter"},
        ], read=3)

        messages = conv_mod.get_conversation(conv["id"])["messages"]
        assistant = [m for m in messages if m["role"] == "assistant"]
        assert assistant, "the turn was thrown away"
        meta = assistant[-1].get("metadata") or {}
        assert meta.get("trace"), "the searches it had already run were lost"
        assert meta.get("interrupted") is True

    def test_a_turn_that_finishes_is_not_marked_interrupted(
            self, client, isolated_db, monkeypatch):
        from carrot import conversation as conv_mod

        conv = conv_mod.create_conversation("finished")
        self._drive(monkeypatch, conv["id"],
                    [{"chunk": "Done."}, {"_final_text": "Done."}])

        assistant = [m for m in conv_mod.get_conversation(conv["id"])["messages"]
                     if m["role"] == "assistant"]
        assert assistant[-1]["content"] == "Done."
        assert not (assistant[-1].get("metadata") or {}).get("interrupted")

    def test_an_interrupted_turn_does_not_feed_the_memory_extractor(
            self, client, isolated_db, monkeypatch):
        """The extractor reads conclusions out of a turn. One cut off halfway
        has none, and filing what it was mid-way through saying is how a
        stopped sentence becomes a durable belief."""
        from carrot import conversation as conv_mod

        called = []
        monkeypatch.setattr(A, "_post_turn", lambda *a, **k: called.append(a))
        conv = conv_mod.create_conversation("half")
        self._drive(monkeypatch, conv["id"], [
            {"chunk": "The user always prefers"},
            {"_final_text": "The user always prefers"},
            {"chunk": " tabs over spaces"},
        ], read=2)
        assert called == [], "a half-finished turn was mined for memories"

    def test_a_turn_with_nothing_at_all_stores_nothing(
            self, client, isolated_db, monkeypatch):
        """An empty row is not a record of anything, and it would show as a
        blank reply under the question."""
        from carrot import conversation as conv_mod

        conv = conv_mod.create_conversation("empty")
        self._drive(monkeypatch, conv["id"], [{"turn_id": "x"}], read=1)
        assert [m for m in conv_mod.get_conversation(conv["id"])["messages"]
                if m["role"] == "assistant"] == []
