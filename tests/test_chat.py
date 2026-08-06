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
