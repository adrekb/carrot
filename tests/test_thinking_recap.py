"""Tests for thinking-trace streaming and the live recap pipeline."""
import json

from carrot.ollama_client import ThinkTagStreamFilter


def _drain(filter_, chunks):
    events = []
    for chunk in chunks:
        events.extend(filter_.feed(chunk))
    events.extend(filter_.flush())
    return events


def _join(events, kind):
    return "".join(e["text"] for e in events if e["type"] == kind)


def test_think_filter_passthrough_without_tags():
    events = _drain(ThinkTagStreamFilter(), ["Hello ", "world"])
    assert _join(events, "content") == "Hello world"
    assert _join(events, "thinking") == ""


def test_think_filter_splits_channels():
    events = _drain(ThinkTagStreamFilter(), ["<think>hmm</think>Answer"])
    assert _join(events, "thinking") == "hmm"
    assert _join(events, "content") == "Answer"


def test_think_filter_tag_split_across_chunks():
    events = _drain(ThinkTagStreamFilter(), ["<thi", "nk>deep", " thought</th", "ink>Done"])
    assert _join(events, "thinking") == "deep thought"
    assert _join(events, "content") == "Done"


def test_think_filter_flushes_unclosed_think():
    events = _drain(ThinkTagStreamFilter(), ["<think>never closed"])
    assert _join(events, "thinking") == "never closed"
    assert _join(events, "content") == ""


def _parse_sse(text):
    payloads = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data:"):
            payloads.append(json.loads(frame[len("data:"):].strip()))
    return payloads


def test_chat_stream_emits_thinking_frames(client):
    resp = client.post("/api/chat/stream", json={"message": "Stream please"})
    assert resp.status_code == 200

    payloads = _parse_sse(resp.text)
    thinking = "".join(p["thinking"] for p in payloads if "thinking" in p)
    content = "".join(p["chunk"] for p in payloads if "chunk" in p)

    assert thinking == "Considering the question."
    assert content == "Hello from Carrot"
    # Thinking must never leak into the persisted assistant message.
    done = next(p for p in payloads if p.get("done"))
    conv = client.get(f"/api/conversations/{done['conversation_id']}").json()
    assistant = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert assistant[-1]["content"] == "Hello from Carrot"


def test_recap_stream_reports_stages_and_tokens(client, monkeypatch, fake_ollama, tmp_path):
    from carrot import recap as recap_mod
    from carrot import deep_research as dr_mod

    monkeypatch.setattr(dr_mod, "OllamaClient", fake_ollama)
    monkeypatch.setattr(dr_mod, "BRIEFINGS_DIR", str(tmp_path / "briefings"))
    monkeypatch.setattr(
        recap_mod, "fetch_feed",
        lambda url: [{"title": "T", "summary": "S", "link": "L", "published": "", "source": "Src"}],
    )

    resp = client.post("/api/recap/run/stream", json={"include_web_search": False})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    payloads = _parse_sse(resp.text)
    stages = [p["stage"] for p in payloads if "stage" in p]
    assert "analyze" in stages
    assert "feeds" in stages
    assert "summarize" in stages

    thinking = "".join(p["thinking"] for p in payloads if "thinking" in p)
    tokens = "".join(p["token"] for p in payloads if "token" in p)
    assert thinking == "weighing stories"
    # The deep-research pipeline prepends a dated briefing header.
    assert tokens.startswith("# Morning Briefing")
    assert tokens.endswith("Recap summary")

    done = next(p for p in payloads if p.get("done"))
    assert done["summary"].endswith("Recap summary")

    # The recap should also be persisted to the DB and a briefing file written.
    recaps = client.get("/api/recap").json()
    assert len(recaps) >= 1
    briefing = client.get("/api/recap/briefing/today").json()
    assert briefing["available"] is True
    assert briefing["markdown"].endswith("Recap summary")
