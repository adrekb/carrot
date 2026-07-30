"""Tests for the hybrid search endpoint (FTS path, embeddings stubbed)."""


def _seed(client, content):
    conv = client.post("/api/conversations", json={"title": "seed"}).json()
    client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"role": "user", "content": content},
    )
    return conv["id"]


def test_search_finds_matching_message(client, monkeypatch):
    from carrot import search as search_mod
    # Force the FTS-only path so no Ollama/embedding calls are attempted.
    monkeypatch.setattr(search_mod, "get_embedding", lambda text: None)

    conv_id = _seed(client, "I love pineapple pizza on Fridays")

    resp = client.get("/api/search", params={"q": "pineapple"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert any(r["conversation_id"] == conv_id for r in data["results"])
    assert any("pineapple" in r["content"] for r in data["results"])


def test_search_no_results(client, monkeypatch):
    from carrot import search as search_mod
    monkeypatch.setattr(search_mod, "get_embedding", lambda text: None)

    _seed(client, "totally unrelated content here")
    resp = client.get("/api/search", params={"q": "zzznonexistentterm"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_search_post_endpoint(client, monkeypatch):
    from carrot import search as search_mod
    monkeypatch.setattr(search_mod, "get_embedding", lambda text: None)

    _seed(client, "remember the quarterly budget review")
    resp = client.post("/api/search", json={"query": "budget"})
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1


def test_classify_query_fallback(client, monkeypatch):
    from carrot import search as search_mod

    class _FakeClient:
        def is_available(self):
            return True

        def classify_query(self, query):
            return {"search_keywords": query, "time_cutoff_days": 0, "intent": "general", "entities": []}

    # search.py binds OllamaClient at import time, so patch it on the module.
    monkeypatch.setattr(search_mod, "OllamaClient", _FakeClient)

    resp = client.post("/api/search/classify", json={"query": "what did I say about gym"})
    assert resp.status_code == 200
    data = resp.json()
    assert "search_keywords" in data
