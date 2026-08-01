"""Tests for interop (Obsidian/editor bridges) and the storage manager."""
import os

import pytest

from carrot import interop
from carrot import notes as notes_mod


@pytest.fixture(autouse=True)
def isolated_notes(tmp_path, monkeypatch):
    """Keep test notes out of the real carrot/data/notes directory."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    monkeypatch.setattr(notes_mod, "NOTES_DIR", str(notes_dir))


# ===== Obsidian: send =====

def _setup_vault(client, tmp_path):
    vault = tmp_path / "MyVault"
    vault.mkdir()
    resp = client.put("/api/interop/vault", json={"vault_path": str(vault)})
    assert resp.status_code == 200 and resp.json()["vault_ok"] is True
    return vault


def test_vault_rejects_missing_folder(client, tmp_path):
    resp = client.put("/api/interop/vault", json={"vault_path": str(tmp_path / "nope")})
    assert resp.status_code == 400


def test_send_note_lands_in_vault_and_updates_in_place(client, tmp_path):
    vault = _setup_vault(client, tmp_path)
    note = client.post("/api/notes", json={"title": "Project: plan/v2", "content": "hello world"}).json()
    r = client.post("/api/interop/obsidian/send", json={"note_id": note["id"]}).json()
    assert r["path"].endswith(".md") and os.path.exists(r["path"])
    assert os.path.dirname(r["path"]).endswith("Carrot")  # tidy subfolder
    assert "/" not in os.path.basename(r["path"]).replace(".md", "")  # sanitized
    text = open(r["path"], encoding="utf-8").read()
    assert "hello world" in text
    assert r["uri"].startswith("obsidian://open?path=")

    # Sending again updates the same file, not a duplicate.
    r2 = client.post("/api/interop/obsidian/send", json={"note_id": note["id"]}).json()
    assert r2["path"] == r["path"]
    files = [f for f in os.listdir(vault / "Carrot") if f.endswith(".md")]
    assert len(files) == 1


def test_send_without_vault_is_a_clear_error(client):
    client.put("/api/interop/vault", json={"vault_path": ""})
    note = client.post("/api/notes", json={"title": "x", "content": "y"}).json()
    resp = client.post("/api/interop/obsidian/send", json={"note_id": note["id"]})
    assert resp.status_code == 400
    assert "vault" in resp.json()["detail"].lower()


# ===== Obsidian: import =====

def test_import_is_idempotent_and_skips_carrot_exports(client, tmp_path, monkeypatch):
    vault = _setup_vault(client, tmp_path)
    monkeypatch.setattr(interop, "IMPORT_LEDGER_PATH", str(tmp_path / "ledger.json"))
    (vault / "Ideas.md").write_text("# Ideas\nbuild a rocket", encoding="utf-8")
    (vault / "sub").mkdir()
    (vault / "sub" / "Diary.md").write_text("today was fine", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "config.md").write_text("internal", encoding="utf-8")
    (vault / "Carrot").mkdir()
    (vault / "Carrot" / "FromCarrot.md").write_text("<!-- carrot:x -->", encoding="utf-8")

    r = client.post("/api/interop/obsidian/import").json()
    assert r == {"imported": 2, "updated": 0, "skipped": 0}

    # Re-run: nothing changed, nothing duplicated.
    r2 = client.post("/api/interop/obsidian/import").json()
    assert r2["imported"] == 0 and r2["skipped"] == 2

    # Touch a file forward in time -> updated in place.
    f = vault / "Ideas.md"
    f.write_text("# Ideas\nbuild TWO rockets", encoding="utf-8")
    os.utime(f, (os.path.getmtime(f) + 5, os.path.getmtime(f) + 5))
    r3 = client.post("/api/interop/obsidian/import").json()
    assert r3["updated"] == 1 and r3["imported"] == 0

    notes = client.get("/api/notes?folder=obsidian").json()
    titles = sorted(n["title"] for n in notes)
    assert titles == ["Diary", "Ideas"]


# ===== Editors =====

def test_editor_preference_order(monkeypatch):
    monkeypatch.setattr(interop.shutil, "which",
                        lambda e: "/bin/" + e if e in ("cursor", "code") else None)
    assert interop.available_editors() == ["cursor", "vscode"]
    assert interop.editor_command()[0] == "cursor"
    assert interop.editor_command(preferred="vscode")[0] == "vscode"
    monkeypatch.setattr(interop.shutil, "which", lambda e: None)
    assert interop.available_editors() == []
    assert interop.editor_command() is None


# ===== Storage manager =====

def test_storage_lists_models_and_marks_active(client, monkeypatch):
    from carrot import app as app_mod
    from carrot import config
    config.set_config("ollama_model", "big:14b")

    class FakeClient:
        def is_available(self): return True
        def list_models(self):
            return [{"name": "small:3b", "size": 2_000_000_000, "modified_at": "2026-07-01T00:00:00Z"},
                    {"name": "big:14b", "size": 9_000_000_000, "modified_at": "2026-07-02T00:00:00Z"}]
    monkeypatch.setattr(app_mod.ollama_mod, "OllamaClient", FakeClient)

    data = client.get("/api/hub/storage").json()
    assert data["models"][0]["name"] == "big:14b"  # sorted by size desc
    assert data["models"][0]["active"] is True
    assert data["models_total_bytes"] == 11_000_000_000
    assert data["disk_free_bytes"] > 0


def test_delete_refuses_active_model(client, monkeypatch):
    from carrot import app as app_mod
    from carrot import config
    config.set_config("ollama_model", "keeper:8b")
    resp = client.post("/api/models/delete", json={"model": "keeper:8b"})
    assert resp.status_code == 400
    assert "active" in resp.json()["detail"].lower()

    deleted = {}

    class FakeClient:
        def is_available(self): return True
        def delete_model(self, m): deleted["m"] = m; return True
    monkeypatch.setattr(app_mod.ollama_mod, "OllamaClient", FakeClient)
    resp = client.post("/api/models/delete", json={"model": "old:3b"})
    assert resp.status_code == 200 and deleted["m"] == "old:3b"
