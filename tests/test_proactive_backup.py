"""Tests for proactive notifications and full export/import."""
import os
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from carrot import backup, config, database, proactive


def _iso(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


# ===== Notification store =====

def test_create_and_list(isolated_db):
    created = proactive.create("test", "Something happened", body="details")
    assert created["title"] == "Something happened"
    assert proactive.unread_count() == 1
    assert [n["id"] for n in proactive.list_notifications()] == [created["id"]]


def test_dedupe_suppresses_repeats(isolated_db):
    assert proactive.create("test", "One", dedupe_key="k") is not None
    assert proactive.create("test", "One again", dedupe_key="k") is None
    assert len(proactive.list_notifications()) == 1


def test_dismissing_allows_a_later_recurrence(isolated_db):
    first = proactive.create("test", "One", dedupe_key="k")
    proactive.dismiss(first["id"])
    assert proactive.create("test", "One", dedupe_key="k") is not None


def test_mark_read_and_dismiss(isolated_db):
    created = proactive.create("test", "Read me")
    assert proactive.mark_read(created["id"]) is True
    assert proactive.unread_count() == 0

    assert proactive.dismiss(created["id"]) is True
    assert proactive.list_notifications() == []
    assert proactive.mark_read("nope") is False


def test_mark_all_read(isolated_db):
    proactive.create("test", "a")
    proactive.create("test", "b")
    assert proactive.mark_all_read() == 2
    assert proactive.unread_count() == 0


def test_unread_only_filter(isolated_db):
    read = proactive.create("test", "read")
    proactive.create("test", "unread")
    proactive.mark_read(read["id"])
    assert [n["title"] for n in proactive.list_notifications(unread_only=True)] == ["unread"]


# ===== Checks =====

def test_overdue_reminder_raises_one_notification(isolated_db):
    from carrot import reminders

    reminders.create_reminder("Pay rent", due_at=_iso(days=-2))
    assert len(proactive.check_overdue_reminders()) == 1
    assert proactive.check_overdue_reminders() == [], "deduped on the second pass"


def test_upcoming_reminder_is_flagged(isolated_db):
    from carrot import reminders

    reminders.create_reminder("Standup", due_at=_iso(hours=2))
    raised = proactive.check_upcoming_reminders()
    assert len(raised) == 1
    assert raised[0]["severity"] == proactive.SEVERITY_WARNING


def test_distant_reminder_is_not_flagged(isolated_db):
    from carrot import reminders

    reminders.create_reminder("Later", due_at=_iso(days=30))
    assert proactive.check_upcoming_reminders() == []


def test_completed_reminders_are_ignored(isolated_db):
    from carrot import reminders

    reminder = reminders.create_reminder("Done", due_at=_iso(days=-1))
    reminders.complete_reminder(reminder["id"])
    assert proactive.check_overdue_reminders() == []


def test_stale_goal_is_nudged(isolated_db):
    from carrot import goals
    from carrot.database import get_db

    goal = goals.create_goal("Bench press", category="fitness")
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    conn = get_db()
    conn.execute("UPDATE goals SET created_at = ? WHERE id = ?", (old, goal["id"]))
    conn.commit()
    conn.close()

    raised = proactive.check_stale_goals()
    assert len(raised) == 1
    assert "Bench press" in raised[0]["title"]


def test_fresh_goal_is_not_nudged(isolated_db):
    from carrot import goals

    goals.create_goal("Just started")
    assert proactive.check_stale_goals() == []


def test_embedding_backlog_only_fires_when_large(isolated_db, monkeypatch):
    from carrot import vectors

    monkeypatch.setattr(vectors, "pending", lambda ns: 10)
    assert proactive.check_embedding_backlog() == []

    monkeypatch.setattr(vectors, "pending", lambda ns: 1000)
    assert len(proactive.check_embedding_backlog()) == 1


def test_run_checks_isolates_failures(isolated_db, monkeypatch):
    """One broken check must not stop the others from running."""
    def explode():
        raise RuntimeError("boom")

    monkeypatch.setitem(proactive.CHECKS, "reminders_overdue", explode)
    result = proactive.run_checks()
    assert "boom" in result["errors"]["reminders_overdue"]
    assert "goals_stale" in result["checked"]


def test_checks_can_be_disabled(isolated_db):
    config.set_config("proactive_disabled_checks", ["goals_stale", "assignments_due"])
    assert "goals_stale" not in proactive.run_checks()["checked"]


# ===== Notification API =====

def test_notification_api_flow(client):
    from carrot import reminders

    reminders.create_reminder("Overdue thing", due_at=_iso(days=-1))
    assert client.post("/api/notifications/check").json()["count"] >= 1

    listed = client.get("/api/notifications").json()
    assert listed["unread"] >= 1
    notification_id = listed["notifications"][0]["id"]

    assert client.post(f"/api/notifications/{notification_id}/read").json()["read"] is True
    assert client.delete(f"/api/notifications/{notification_id}").json()["dismissed"] is True
    assert client.delete(f"/api/notifications/{notification_id}").status_code == 404


def test_read_all_endpoint(client):
    proactive.create("test", "a")
    proactive.create("test", "b")
    assert client.post("/api/notifications/read-all").json()["marked"] == 2


# ===== Backup =====

@pytest.fixture
def instance(tmp_path, isolated_db, monkeypatch):
    """A Carrot instance with some data and a temp data root."""
    from carrot import conversation as conv_mod, memory, vectors

    monkeypatch.setattr(vectors, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(backup, "_data_root", lambda: str(tmp_path))

    conv = conv_mod.create_conversation(title="a chat")
    conv_mod.add_message(conv["id"], "user", "remember the milk")
    memory.create("fact", "milk", "The user needs milk.")

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "note.md").write_text("# A note\n\nContents.")
    return tmp_path


def test_export_produces_a_readable_archive(instance):
    result = backup.export_archive()
    assert os.path.exists(result["path"])
    assert result["manifest"]["counts"]["conversations"] == 1

    with zipfile.ZipFile(result["path"]) as archive:
        names = archive.namelist()
    assert backup.DB_ENTRY in names
    assert "data/notes/note.md" in names


def test_export_can_omit_vectors(instance):
    from carrot import vectors

    vectors.store(vectors.NS_MEMORY, "x", [1.0, 2.0])
    lean = backup.export_archive(include_vectors=False)
    assert lean["manifest"]["includes_vectors"] is False


def test_inspect_valid_archive(instance):
    exported = backup.export_archive()
    inspection = backup.inspect_archive(exported["path"])
    assert inspection["valid"] is True
    assert inspection["manifest"]["format_version"] == backup.ARCHIVE_FORMAT_VERSION


def test_inspect_missing_file(instance):
    assert backup.inspect_archive("/nonexistent.zip")["valid"] is False


def test_inspect_rejects_a_non_carrot_zip(instance, tmp_path):
    stray = tmp_path / "other.zip"
    with zipfile.ZipFile(stray, "w") as archive:
        archive.writestr("hello.txt", "not a backup")
    assert "no manifest" in backup.inspect_archive(str(stray))["error"]


def test_inspect_rejects_a_future_format(instance, tmp_path):
    import json

    future = tmp_path / "future.zip"
    with zipfile.ZipFile(future, "w") as archive:
        archive.writestr(backup.MANIFEST_NAME, json.dumps({"format_version": 999}))
        archive.writestr(backup.DB_ENTRY, b"")
    assert "newer than this build" in backup.inspect_archive(str(future))["error"]


def test_import_restores_exported_data(instance):
    from carrot import conversation as conv_mod, memory

    exported = backup.export_archive()

    # Wipe the instance, then restore it.
    conn = database.get_db()
    conn.execute("DELETE FROM conversations")
    conn.execute("DELETE FROM memories")
    conn.commit()
    conn.close()
    assert conv_mod.list_conversations() == []

    result = backup.import_archive(exported["path"], safety_copy=False)
    assert result["success"] is True
    assert len(conv_mod.list_conversations()) == 1
    assert len(memory.list_memories()) == 1


def test_import_restores_note_files(instance):
    exported = backup.export_archive()
    (instance / "notes" / "note.md").unlink()

    backup.import_archive(exported["path"], safety_copy=False)
    assert (instance / "notes" / "note.md").read_text().startswith("# A note")


def test_import_takes_a_safety_copy(instance):
    exported = backup.export_archive()
    result = backup.import_archive(exported["path"], safety_copy=True)
    assert os.path.exists(result["safety_copy"])


def test_import_rejects_path_traversal(instance, tmp_path):
    """A crafted archive must not be able to write outside the data directory."""
    import json

    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr(backup.MANIFEST_NAME, json.dumps({"format_version": 1}))
        archive.writestr(backup.DB_ENTRY, b"")
        archive.writestr("../../../etc/pwned", "owned")

    result = backup.import_archive(str(evil), safety_copy=False)
    assert result["success"] is False
    assert "unsafe paths" in result["error"]


def test_import_rejects_a_corrupt_database(instance, tmp_path):
    """A truncated archive must not destroy the working instance."""
    import json

    from carrot import conversation as conv_mod

    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr(backup.MANIFEST_NAME, json.dumps({"format_version": 1}))
        archive.writestr(backup.DB_ENTRY, b"this is not a database")

    result = backup.import_archive(str(broken), safety_copy=False)
    assert result["success"] is False
    assert len(conv_mod.list_conversations()) == 1, "the original data survived"


def test_list_backups(instance):
    backup.export_archive()
    backups = backup.list_backups()
    assert len(backups) == 1
    assert backups[0]["size_bytes"] > 0


def test_backup_api_roundtrip(client, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "_data_root", lambda: str(tmp_path))
    client.post("/api/conversations", json={"title": "keep me"})

    exported = client.post("/api/backup/export", json={}).json()
    assert client.post("/api/backup/inspect", json={"path": exported["path"]}).json()["valid"]

    listed = client.get("/api/backup").json()["backups"]
    assert len(listed) == 1

    result = client.post(
        "/api/backup/import", json={"path": exported["path"], "safety_copy": False}
    ).json()
    assert result["success"] is True


def test_import_api_rejects_a_bad_path(client):
    assert client.post("/api/backup/import", json={"path": "/nope.zip"}).status_code == 400
