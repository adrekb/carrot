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


# ===== The harmony channel format =====
#
# Reported against a real answer: a well-sourced reply about the Corvette ZR1X
# arrived with the model's own planning inside it. The cause was not a missing
# marker — `<|channel|>analysis` was in the opener list — but `<|message|>`
# being in the *closer* list. It is the token that follows the opener, so
# thinking opened and closed one token later, and the whole chain of thought
# was emitted as the answer.
#
# Every one of these is a chunk-size sweep. The filter buffers, and a bug that
# only appears when a marker straddles a boundary is a bug that only appears
# against a real provider.

HARMONY = ("<|start|>assistant<|channel|>analysis<|message|>"
           "The user asks about the ZR1X. I should structure this."
           "<|end|><|start|>assistant<|channel|>final<|message|>"
           "The ZR1X is a 2026 model year vehicle.")

CHUNK_SIZES = (1, 3, 7, 19, 1000)


def _sweep(text):
    """Feed `text` at several chunk sizes; return (content, thinking) per size."""
    out = {}
    for size in CHUNK_SIZES:
        events = _drain(ThinkTagStreamFilter(),
                        [text[i:i + size] for i in range(0, len(text), size)])
        out[size] = (_join(events, "content"), _join(events, "thinking"))
    return out


def test_harmony_reasoning_does_not_reach_the_answer():
    for size, (content, _) in _sweep(HARMONY).items():
        assert content == "The ZR1X is a 2026 model year vehicle.", f"chunk={size}"


def test_harmony_reasoning_is_kept_as_thinking():
    """Routed, not discarded — the trace pane is where it belongs."""
    for size, (_, thinking) in _sweep(HARMONY).items():
        assert "I should structure this" in thinking, f"chunk={size}"


def test_harmony_control_tokens_are_dropped_not_printed():
    """`<|start|>assistant` is neither an opener nor a closer.

    Under the old two-list model anything unmatched was prose by default, so
    the scaffolding was printed to the user alongside the answer.
    """
    for size, (content, _) in _sweep(HARMONY).items():
        for token in ("<|start|>", "<|message|>", "<|end|>", "assistant"):
            assert token not in content, f"chunk={size} leaked {token}"


def test_the_single_pipe_channel_variant_is_handled():
    """Seen in the wild from a local build: `<|channel>` with one pipe."""
    text = ("<|channel>thought<|message|>hidden working"
            "<|channel>final<|message|>Visible answer.")
    for size, (content, thinking) in _sweep(text).items():
        assert content == "Visible answer.", f"chunk={size}"
        assert "hidden working" in thinking, f"chunk={size}"


def test_an_answer_that_merely_mentions_a_marker_word_is_untouched():
    """The filter matches tokens, not vocabulary.

    A reply about prompt formats will contain the word "analysis" and must
    not lose everything after it.
    """
    text = "The analysis shows a final result. No markers here."
    for size, (content, thinking) in _sweep(text).items():
        assert content == text, f"chunk={size}"
        assert thinking == "", f"chunk={size}"


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
