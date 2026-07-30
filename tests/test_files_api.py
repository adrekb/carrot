"""Tests for the sandboxed file API backing the Code tab."""
import os
import pytest


@pytest.fixture
def workspace(client, tmp_path):
    """Point the file API at a temporary workspace root with sample content."""
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "sub" / "notes.txt").write_text("nested", encoding="utf-8")
    resp = client.post("/api/files/root", json={"root": str(root)})
    assert resp.status_code == 200
    return root


def test_root_get_and_set(client, workspace):
    resp = client.get("/api/files/root")
    assert resp.status_code == 200
    assert resp.json()["root"] == str(workspace)


def test_tree_lists_entries(client, workspace):
    data = client.get("/api/files/tree", params={"path": ""}).json()
    names = {e["name"]: e for e in data["entries"]}
    assert "hello.py" in names
    assert "sub" in names
    assert names["sub"]["is_dir"] is True
    assert names["hello.py"]["is_dir"] is False


def test_tree_nested(client, workspace):
    data = client.get("/api/files/tree", params={"path": "sub"}).json()
    assert [e["name"] for e in data["entries"]] == ["notes.txt"]


def test_read_file(client, workspace):
    data = client.get("/api/files/read", params={"path": "hello.py"}).json()
    assert data["content"] == "print('hi')\n"


def test_write_file_roundtrip(client, workspace):
    resp = client.post("/api/files/write", json={"path": "new.md", "content": "# Title\n"})
    assert resp.status_code == 200
    assert (workspace / "new.md").read_text(encoding="utf-8") == "# Title\n"
    # And it reads back.
    data = client.get("/api/files/read", params={"path": "new.md"}).json()
    assert data["content"] == "# Title\n"


def test_write_creates_subdirectories(client, workspace):
    resp = client.post("/api/files/write", json={"path": "a/b/c.txt", "content": "deep"})
    assert resp.status_code == 200
    assert (workspace / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "deep"


@pytest.mark.parametrize("escape", ["../secret.txt", "..%2Fsecret.txt", "sub/../../outside.txt"])
def test_path_traversal_rejected(client, workspace, escape):
    from urllib.parse import unquote
    path = unquote(escape)
    # Read attempts must be rejected with 403.
    r = client.get("/api/files/read", params={"path": path})
    assert r.status_code == 403
    # Write attempts must be rejected with 403 and create nothing outside.
    w = client.post("/api/files/write", json={"path": path, "content": "x"})
    assert w.status_code == 403
    assert not (workspace.parent / "secret.txt").exists()
    assert not (workspace.parent / "outside.txt").exists()


def test_read_missing_file_404(client, workspace):
    assert client.get("/api/files/read", params={"path": "nope.txt"}).status_code == 404
