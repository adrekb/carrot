"""Tests for chat endpoints (streaming and non-streaming) and conversations."""
import json


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

    def setup_method(self):
        from carrot import config
        for key in ("answer_style", "answer_structure", "answer_custom"):
            config.set_config(key, "")

    def teardown_method(self):
        self.setup_method()

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
