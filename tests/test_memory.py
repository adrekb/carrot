"""Tests for structured memory, rolling summaries, and the vector store."""
import json

import pytest

from carrot import memory, summarize, vectors


# ===== Memory CRUD and supersession =====

def test_create_and_list(isolated_db):
    created = memory.create("preference", "Editor", "The user prefers Neovim.", confidence=0.9)
    assert created["subject"] == "editor", "subjects are normalized to lowercase"
    assert created["status"] == "active"

    listed = memory.list_memories()
    assert [m["id"] for m in listed] == [created["id"]]


def test_new_value_supersedes_old_and_keeps_history(isolated_db):
    first = memory.create("preference", "editor", "The user prefers Neovim.")
    second = memory.create("preference", "editor", "The user prefers Zed.")

    assert memory.get(first["id"])["status"] == "superseded"
    assert memory.get(first["id"])["superseded_by"] == second["id"]
    assert [m["id"] for m in memory.list_memories()] == [second["id"]]

    history = memory.history("editor")
    assert [m["content"] for m in history] == [
        "The user prefers Neovim.",
        "The user prefers Zed.",
    ]


def test_history_order_survives_a_shared_timestamp(isolated_db):
    """Two writes in the same clock tick must still read oldest-first.

    `created_at` is microsecond ISO text, but the clock behind it ticks in
    milliseconds on Windows — so this is not a hypothetical. With no tiebreak
    the ordering was whatever SQLite felt like, and the suite flaked here.
    """
    from unittest.mock import patch

    with patch.object(memory, "_now", lambda: "2026-08-06T10:00:00+00:00"):
        memory.create("preference", "editor", "The user prefers Neovim.")
        memory.create("preference", "editor", "The user prefers Zed.")

    assert [m["content"] for m in memory.history("editor")] == [
        "The user prefers Neovim.",
        "The user prefers Zed.",
    ]


def test_different_kinds_do_not_collide(isolated_db):
    memory.create("preference", "python", "The user enjoys Python.")
    memory.create("project", "python", "The user is building a Python CLI.")
    assert len(memory.list_memories()) == 2


def test_update_and_reject(isolated_db):
    created = memory.create("fact", "gym", "The user goes to the gym on Mondays.")
    updated = memory.update(created["id"], content="The user goes to the gym daily.", pinned=True)
    assert updated["content"] == "The user goes to the gym daily."
    assert updated["pinned"] is True

    rejected = memory.reject(created["id"])
    assert rejected["status"] == "rejected"
    assert memory.list_memories() == []


def test_delete(isolated_db):
    created = memory.create("fact", "car", "The user drives a hatchback.")
    assert memory.delete(created["id"]) is True
    assert memory.get(created["id"]) is None
    assert memory.delete(created["id"]) is False


def test_provenance_is_recorded(isolated_db):
    created = memory.create(
        "fact", "bench", "The user benches 185lbs.",
        source_message_id=42, source_conversation_id="conv-1",
    )
    assert created["source_message_id"] == 42
    assert created["source_conversation_id"] == "conv-1"


# ===== Search =====

def test_search_finds_by_keyword(isolated_db, monkeypatch):
    monkeypatch.setattr(vectors, "search_text", lambda *a, **k: [])
    memory.create("fact", "bench", "The user benches 185 pounds.")
    memory.create("fact", "commute", "The user cycles to work.")

    results = memory.search("bench")
    assert len(results) == 1
    assert "185" in results[0]["content"]


def test_search_tolerates_fts_operators(isolated_db, monkeypatch):
    """Raw user text goes into MATCH, so operators must not blow up the query."""
    monkeypatch.setattr(vectors, "search_text", lambda *a, **k: [])
    memory.create("fact", "quote", 'The user said "hello" once.')

    for query in ['"unbalanced', "NEAR(a b)", "a AND OR b", "*", ""]:
        assert isinstance(memory.search(query), list)


def test_recall_always_includes_pinned(isolated_db, monkeypatch):
    monkeypatch.setattr(vectors, "search_text", lambda *a, **k: [])
    memory.create("preference", "tone", "The user prefers terse answers.", pinned=True)
    memory.create("fact", "pet", "The user has a rabbit.")

    recalled = memory.recall("something unrelated to either")
    assert any(m["subject"] == "tone" for m in recalled)


def test_prompt_block_is_empty_without_memories(isolated_db):
    assert memory.as_prompt_block([]) == ""


def test_prompt_block_renders_memories(isolated_db):
    created = memory.create("preference", "editor", "The user prefers Neovim.")
    block = memory.as_prompt_block([created])
    assert "The user prefers Neovim." in block
    assert "preference" in block


# ===== Extraction =====

class _ExtractorClient:
    """Stands in for Ollama, returning a fixed extraction payload."""

    def __init__(self, payload):
        self.payload = payload

    def is_available(self):
        return True

    def structured_chat(self, messages, model=None, response_format=None):
        return json.dumps(self.payload)


def _patch_extractor(monkeypatch, payload):
    import carrot.ollama_client as ollama_client

    monkeypatch.setattr(ollama_client, "OllamaClient", lambda: _ExtractorClient(payload))


def test_extraction_stores_confident_memories(isolated_db, monkeypatch):
    _patch_extractor(monkeypatch, {"memories": [
        {"kind": "preference", "subject": "editor", "content": "The user prefers Vim.", "confidence": 0.9},
    ]})
    stored = memory.extract_from_turn("I always use Vim", conversation_id="c1", message_id=7)
    assert len(stored) == 1
    assert stored[0]["source_conversation_id"] == "c1"


def test_extraction_drops_low_confidence(isolated_db, monkeypatch):
    _patch_extractor(monkeypatch, {"memories": [
        {"kind": "fact", "subject": "maybe", "content": "The user might like jazz.", "confidence": 0.2},
    ]})
    assert memory.extract_from_turn("maybe jazz?") == []


def test_extraction_skips_rejected_subjects(isolated_db, monkeypatch):
    """A subject the user rejected must not come back on the next turn."""
    rejected = memory.create("fact", "shoes", "The user wears size 12.")
    memory.reject(rejected["id"])

    _patch_extractor(monkeypatch, {"memories": [
        {"kind": "fact", "subject": "shoes", "content": "The user wears size 12.", "confidence": 0.95},
    ]})
    assert memory.extract_from_turn("my shoes are size 12") == []


def test_extraction_skips_duplicates(isolated_db, monkeypatch):
    payload = {"memories": [
        {"kind": "fact", "subject": "pet", "content": "The user has a rabbit.", "confidence": 0.9},
    ]}
    _patch_extractor(monkeypatch, payload)
    assert len(memory.extract_from_turn("I have a rabbit")) == 1
    assert memory.extract_from_turn("I have a rabbit") == []


def test_extraction_survives_bad_json(isolated_db, monkeypatch):
    class BadClient:
        def is_available(self):
            return True

        def structured_chat(self, *a, **k):
            return "not json at all"

    import carrot.ollama_client as ollama_client
    monkeypatch.setattr(ollama_client, "OllamaClient", lambda: BadClient())
    assert memory.extract_from_turn("hello") == []


def test_extraction_noop_when_ollama_down(isolated_db, monkeypatch):
    class DownClient:
        def is_available(self):
            return False

    import carrot.ollama_client as ollama_client
    monkeypatch.setattr(ollama_client, "OllamaClient", lambda: DownClient())
    assert memory.extract_from_turn("hello") == []


# ===== Rolling summaries =====

def _seed_conversation(count):
    from carrot import conversation as conv_mod

    conv = conv_mod.create_conversation(title="long")
    for i in range(count):
        conv_mod.add_message(conv["id"], "user" if i % 2 == 0 else "assistant", f"message {i}")
    return conv_mod.get_conversation(conv["id"])


def test_short_conversation_is_not_summarized(isolated_db, monkeypatch):
    conv = _seed_conversation(5)
    assert summarize.maybe_summarize(conv["id"]) is None


def test_long_conversation_folds_into_a_summary(isolated_db, monkeypatch):
    class SummaryClient:
        def is_available(self):
            return True

        def chat(self, messages, model=None):
            return "The user counted from 0 upward."

    import carrot.ollama_client as ollama_client
    monkeypatch.setattr(ollama_client, "OllamaClient", lambda: SummaryClient())

    conv = _seed_conversation(40)
    summary = summarize.maybe_summarize(conv["id"])
    assert summary["summary"] == "The user counted from 0 upward."
    # Everything past the recent window has been absorbed.
    assert summary["message_count"] == 40 - summarize.RECENT_WINDOW


def test_build_history_prepends_summary(isolated_db):
    conv = _seed_conversation(30)
    summarize.save_summary(conv["id"], "Earlier: the user counted.", covered_through=5, message_count=10)

    history = summarize.build_history(conv)
    assert history[0]["role"] == "system"
    assert "Earlier: the user counted." in history[0]["content"]
    assert len(history) == summarize.RECENT_WINDOW + 1


def test_build_history_without_summary_is_just_recent_turns(isolated_db):
    conv = _seed_conversation(30)
    history = summarize.build_history(conv)
    assert len(history) == summarize.RECENT_WINDOW
    assert all(m["role"] != "system" for m in history)


def test_build_history_handles_unsaved_conversation(isolated_db):
    """A dict with no id (never persisted) must not raise."""
    assert summarize.build_history({"messages": [{"role": "user", "content": "hi"}]}) == [
        {"role": "user", "content": "hi"}
    ]


# ===== Vector store =====

def test_pack_roundtrip():
    packed = vectors.pack([0.5, -1.5, 2.0])
    assert [round(v, 3) for v in vectors.unpack(packed)] == [0.5, -1.5, 2.0]


def test_store_and_search(isolated_db):
    vectors.store(vectors.NS_MEMORY, "a", [1.0, 0.0, 0.0])
    vectors.store(vectors.NS_MEMORY, "b", [0.0, 1.0, 0.0])
    vectors.store(vectors.NS_MEMORY, "c", [0.9, 0.1, 0.0])

    ranked = vectors.search(vectors.NS_MEMORY, [1.0, 0.0, 0.0], limit=2)
    assert [ref for ref, _ in ranked] == ["a", "c"]
    assert ranked[0][1] == pytest.approx(1.0, abs=1e-5)


def test_search_restricted_to_candidates(isolated_db):
    vectors.store(vectors.NS_MEMORY, "a", [1.0, 0.0])
    vectors.store(vectors.NS_MEMORY, "b", [0.0, 1.0])
    ranked = vectors.search(vectors.NS_MEMORY, [1.0, 0.0], candidates=["b"])
    assert [ref for ref, _ in ranked] == ["b"]


def test_search_ignores_mismatched_dimensions(isolated_db):
    """A swapped embedding model leaves stale vectors; they must not crash search."""
    vectors.store(vectors.NS_MEMORY, "old", [1.0, 0.0])
    vectors.store(vectors.NS_MEMORY, "new_a", [1.0, 0.0, 0.0])
    vectors.store(vectors.NS_MEMORY, "new_b", [0.0, 0.0, 1.0])

    ranked = vectors.search(vectors.NS_MEMORY, [1.0, 0.0, 0.0])
    assert "old" not in [ref for ref, _ in ranked]


def test_search_with_wrong_query_dimension_returns_empty(isolated_db):
    vectors.store(vectors.NS_MEMORY, "a", [1.0, 0.0, 0.0])
    assert vectors.search(vectors.NS_MEMORY, [1.0, 0.0]) == []


def test_store_replaces_existing_vector(isolated_db):
    vectors.store(vectors.NS_MEMORY, "a", [1.0, 0.0])
    vectors.store(vectors.NS_MEMORY, "a", [0.0, 1.0])
    assert vectors.count(vectors.NS_MEMORY) == 1
    assert list(vectors.get(vectors.NS_MEMORY, "a")) == [0.0, 1.0]


def test_delete_and_count(isolated_db):
    vectors.store(vectors.NS_CHUNK, "1", [1.0])
    vectors.store(vectors.NS_CHUNK, "2", [1.0])
    assert vectors.count(vectors.NS_CHUNK) == 2
    vectors.delete(vectors.NS_CHUNK, "1")
    assert vectors.count(vectors.NS_CHUNK) == 1
    vectors.delete_namespace(vectors.NS_CHUNK)
    assert vectors.count(vectors.NS_CHUNK) == 0


def test_pending_counts_unembedded_rows(isolated_db):
    memory.create("fact", "x", "A thing is true.")
    assert vectors.pending(vectors.NS_MEMORY) == 1
    vectors.store(vectors.NS_MEMORY, memory.list_memories()[0]["id"], [1.0])
    assert vectors.pending(vectors.NS_MEMORY) == 0


@pytest.fixture
def no_background_embedding(monkeypatch):
    """Silence the async embed worker so backfill assertions are deterministic."""
    monkeypatch.setattr(vectors, "enqueue", lambda *a, **k: None)


def test_backfill_embeds_pending_rows(isolated_db, no_background_embedding, monkeypatch):
    monkeypatch.setattr(vectors, "embed", lambda text, model=vectors.EMBEDDING_MODEL: [1.0, 2.0])
    memory.create("fact", "a", "First fact.")
    memory.create("fact", "b", "Second fact.")

    result = vectors.backfill(vectors.NS_MEMORY)
    assert result["embedded"] == 2
    assert result["remaining"] == 0


def test_backfill_stops_when_model_unavailable(isolated_db, no_background_embedding, monkeypatch):
    """One failed call, not `limit` of them, when the embedding model is down."""
    calls = {"n": 0}

    def failing_embed(text, model=vectors.EMBEDDING_MODEL):
        calls["n"] += 1
        return None

    monkeypatch.setattr(vectors, "embed", failing_embed)
    for i in range(5):
        memory.create("fact", f"s{i}", f"Fact number {i}.")

    result = vectors.backfill(vectors.NS_MEMORY)
    assert result["embedded"] == 0
    assert calls["n"] == 1
    assert "unavailable" in result["error"]


def test_backfill_unknown_namespace(isolated_db):
    assert vectors.backfill("nonsense")["error"] == "unknown namespace"


def test_stats_reports_backend(isolated_db):
    stats = vectors.stats()
    assert stats["backend"] in ("numpy", "sqlite-vec")
    assert {n["namespace"] for n in stats["namespaces"]} == set(vectors.BACKFILL_SOURCES)


def test_legacy_embeddings_migrate(isolated_db):
    """Old JSON message_embeddings rows move into the packed vectors table."""
    from carrot import conversation as conv_mod
    from carrot.database import get_db

    conv = conv_mod.create_conversation(title="legacy")
    message = conv_mod.add_message(conv["id"], "user", "an old message")

    conn = get_db()
    conn.execute(
        "INSERT INTO message_embeddings (message_id, model, embedding) VALUES (?, ?, ?)",
        (message["id"], "nomic-embed-text", json.dumps([0.1, 0.2, 0.3])),
    )
    conn.commit()
    conn.close()

    assert vectors.migrate_legacy_embeddings() == 1
    stored = vectors.get(vectors.NS_MESSAGE, str(message["id"]))
    assert [round(float(v), 3) for v in stored] == [0.1, 0.2, 0.3]


# ===== API surface =====

def test_memory_api_crud(client):
    created = client.post("/api/memory", json={
        "kind": "preference", "subject": "editor", "content": "The user prefers Neovim.",
    }).json()

    listed = client.get("/api/memory").json()
    assert listed["stats"]["total"] == 1

    updated = client.put(f"/api/memory/{created['id']}", json={"pinned": True}).json()
    assert updated["pinned"] is True

    assert client.post(f"/api/memory/{created['id']}/reject").json()["status"] == "rejected"
    assert client.delete(f"/api/memory/{created['id']}").json()["deleted"] is True
    assert client.delete(f"/api/memory/{created['id']}").status_code == 404


def test_memory_history_endpoint(client):
    client.post("/api/memory", json={"kind": "fact", "subject": "car", "content": "A hatchback."})
    client.post("/api/memory", json={"kind": "fact", "subject": "car", "content": "An estate."})

    versions = client.get("/api/memory/history/car").json()["versions"]
    assert [v["content"] for v in versions] == ["A hatchback.", "An estate."]


def test_vector_stats_endpoint(client):
    assert client.get("/api/vectors/stats").json()["total"] == 0


class TestMemoryCanBeLeftOutOfOneTurn:
    """Asked for the news and told about a dog mentioned once months ago.

    There was only a global switch, which is the wrong shape for that: you do
    not want memory off, you want it off for this. So the flag rides on the
    turn, and the global setting is what it falls back to.
    """

    def _systems(self, memory):
        from carrot import app as A, conversation as conv_mod

        conv = conv_mod.create_conversation("news")
        history, _ = A._prepare_history(conv, "recent us political news", None,
                                        memory=memory)
        return " ".join(m["content"] for m in history if m["role"] == "system")

    # No teardown restoring memory_enabled, deliberately. `isolated_db` is
    # function-scoped and config lives in that database, so switching the
    # setting off inside a test cannot outlive it. A fixture that put it back
    # ran *after* isolated_db's monkeypatch had been undone, which wrote the
    # setting to the real config instead — a leak invented to fix one that
    # was not there.

    def _remember_the_dog(self):
        from carrot import config, memory as memory_mod

        config.set_config("memory_enabled", True)
        memory_mod.create("fact", "dog", "The user has a dog called Biscuit.")

    def test_by_default_memory_is_used(self, isolated_db):
        self._remember_the_dog()
        assert "Biscuit" in self._systems(None)

    def test_a_turn_can_leave_it_out(self, isolated_db):
        self._remember_the_dog()
        assert "Biscuit" not in self._systems(False)

    def test_the_global_setting_is_still_the_default(self, isolated_db):
        from carrot import config

        self._remember_the_dog()
        config.set_config("memory_enabled", False)
        assert "Biscuit" not in self._systems(None)

    def test_a_turn_can_opt_back_in(self, isolated_db):
        # Otherwise the per-turn flag could only ever subtract, and a chat that
        # genuinely wants context could not have it without a trip to Settings.
        from carrot import config

        self._remember_the_dog()
        config.set_config("memory_enabled", False)
        assert "Biscuit" in self._systems(True)
