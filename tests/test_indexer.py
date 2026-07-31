"""Tests for local document indexing and unified search."""
import os

import pytest

from carrot import indexer, vectors


@pytest.fixture
def corpus(tmp_path, isolated_db, monkeypatch):
    """A small directory tree registered as an index root."""
    monkeypatch.setattr(vectors, "enqueue", lambda *a, **k: None)
    root = tmp_path / "corpus"
    (root / "node_modules").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "notes").mkdir()

    (root / "paper.md").write_text("# Attention\n\n" + "Scaled dot-product attention over keys. " * 60)
    (root / "app.py").write_text("def hello():\n    return 'kangaroo pipeline'\n")
    (root / "notes" / "meeting.txt").write_text("We decided to use SQLite for storage.")
    (root / "page.html").write_text(
        "<html><body><script>var x=1</script><p>Photosynthesis converts light.</p></body></html>"
    )
    (root / "node_modules" / "junk.js").write_text("should never be indexed")
    (root / ".git" / "config").write_text("should never be indexed")
    (root / "image.bin").write_bytes(b"\x00\x01\x02")

    indexer.add_index_dir(str(root))
    return root


# ===== Chunking =====

def test_short_text_is_one_chunk():
    assert indexer.chunk_text("hello world") == ["hello world"]


def test_empty_text_yields_no_chunks():
    assert indexer.chunk_text("") == []
    assert indexer.chunk_text("   ") == []


def test_long_text_is_split_with_overlap():
    text = "\n\n".join(f"Paragraph number {i} with some filler text." for i in range(200))
    chunks = indexer.chunk_text(text, size=500, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 600 for c in chunks)
    # Every chunk carries content; nothing is dropped between boundaries.
    assert "Paragraph number 0" in chunks[0]
    assert "Paragraph number 199" in chunks[-1]


def test_chunking_terminates_on_unbroken_text():
    """Text with no paragraph or sentence breaks must not loop forever."""
    chunks = indexer.chunk_text("x" * 5000, size=1000, overlap=100)
    assert len(chunks) >= 5


# ===== Extraction =====

def test_extract_text_reads_plain_files(tmp_path):
    path = tmp_path / "a.md"
    path.write_text("# Title\n\nBody text.")
    assert "Body text." in indexer.extract_text(str(path))


def test_extract_html_strips_scripts(tmp_path):
    path = tmp_path / "a.html"
    path.write_text("<html><body><script>secret()</script><p>Visible.</p></body></html>")
    text = indexer.extract_text(str(path))
    assert "Visible." in text
    assert "secret" not in text


def test_extract_unsupported_returns_empty(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"\x00\x01")
    assert indexer.extract_text(str(path)) == ""


# ===== Scanning =====

def test_scan_indexes_supported_files_only(corpus):
    state = indexer.run_scan()
    assert state["indexed"] == 4, "md, py, txt and html — not the binary or skipped dirs"

    paths = [os.path.basename(d["path"]) for d in indexer.list_documents()]
    assert set(paths) == {"paper.md", "app.py", "meeting.txt", "page.html"}
    assert "junk.js" not in paths


def test_rescan_is_incremental(corpus):
    indexer.run_scan()
    second = indexer.run_scan()
    assert second["indexed"] == 0, "unchanged files are not re-parsed"


def test_edited_file_is_reindexed(corpus):
    indexer.run_scan()
    (corpus / "app.py").write_text("def hello():\n    return 'wallaby pipeline'\n")
    # Force a different mtime so the fingerprint check sees the edit.
    os.utime(corpus / "app.py", (0, 0))

    assert indexer.run_scan()["indexed"] == 1
    results = indexer.search_documents("wallaby")["results"]
    assert results and "wallaby" in results[0]["content"]


def test_deleted_file_is_pruned(corpus):
    indexer.run_scan()
    os.remove(corpus / "app.py")
    indexer.run_scan()

    paths = [os.path.basename(d["path"]) for d in indexer.list_documents()]
    assert "app.py" not in paths


def test_remove_document_clears_chunks(corpus):
    indexer.run_scan()
    document = next(d for d in indexer.list_documents() if d["path"].endswith("paper.md"))
    assert indexer.remove_document(document["path"]) is True
    assert indexer.stats()["documents"] == 3


# ===== Search =====

def test_search_finds_content_across_file_types(corpus):
    indexer.run_scan()
    assert indexer.search_documents("attention")["results"][0]["title"] == "paper.md"
    assert indexer.search_documents("kangaroo")["results"][0]["title"] == "app.py"
    assert indexer.search_documents("SQLite")["results"][0]["title"] == "meeting.txt"
    assert indexer.search_documents("photosynthesis")["results"][0]["title"] == "page.html"


def test_search_with_no_match(corpus):
    indexer.run_scan()
    assert indexer.search_documents("zzzznonexistent")["count"] == 0


def test_search_tolerates_operator_characters(corpus):
    indexer.run_scan()
    for query in ['"unbalanced', "NEAR(a b)", "*", ""]:
        assert isinstance(indexer.search_documents(query)["results"], list)


def test_search_reranks_with_embeddings(corpus, monkeypatch):
    """When embeddings are available the mode flips from fts_only to hybrid."""
    indexer.run_scan()
    chunk_ids = [str(r["chunk_id"]) for r in indexer.search_documents("attention")["results"]]
    monkeypatch.setattr(
        vectors, "search_text",
        lambda ns, text, limit=20, candidates=None: [(chunk_ids[0], 0.99)],
    )
    assert indexer.search_documents("attention")["mode"] == "hybrid"


# ===== Configuration =====

def test_add_and_remove_index_dir(tmp_path, isolated_db):
    path = tmp_path / "docs"
    path.mkdir()
    assert str(path) in indexer.add_index_dir(str(path))
    assert indexer.remove_index_dir(str(path)) == []


def test_add_index_dir_rejects_non_directory(tmp_path, isolated_db):
    path = tmp_path / "file.txt"
    path.write_text("hi")
    with pytest.raises(ValueError):
        indexer.add_index_dir(str(path))


# ===== API =====

def test_index_api_flow(client, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "readme.md").write_text("The deployment runbook lives here.")

    assert client.post("/api/index/dirs", json={"path": str(root)}).status_code == 200
    indexer.run_scan()

    status = client.get("/api/index/status").json()
    assert status["stats"]["documents"] == 1

    results = client.get("/api/index/search", params={"q": "runbook"}).json()["results"]
    assert results and "runbook" in results[0]["content"]

    assert client.delete("/api/index/dirs", params={"path": str(root)}).json()["dirs"] == []


def test_scan_without_dirs_is_rejected(client):
    assert client.post("/api/index/scan", json={}).status_code == 400


def test_unified_search_covers_every_store(client, tmp_path, monkeypatch):
    from carrot import memory, search as search_mod

    monkeypatch.setattr(search_mod, "get_embedding", lambda text: None)
    monkeypatch.setattr(vectors, "search_text", lambda *a, **k: [])

    root = tmp_path / "docs"
    root.mkdir()
    (root / "notes.md").write_text("Kubernetes deployment notes.")
    client.post("/api/index/dirs", json={"path": str(root)})
    indexer.run_scan()

    conv = client.post("/api/conversations", json={"title": "k8s"}).json()
    client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"role": "user", "content": "How do I do a Kubernetes rollout?"},
    )
    memory.create("project", "kubernetes", "The user runs a Kubernetes cluster.")

    data = client.get("/api/search/all", params={"q": "kubernetes"}).json()
    assert data["documents"], "document index searched"
    assert data["conversations"], "conversations searched"
    assert data["memories"], "memory searched"
