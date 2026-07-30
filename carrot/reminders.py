import json
import uuid
from datetime import datetime, timezone

from carrot.database import get_db


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_reminder(title: str, description: str = "", due_at: str = None, metadata: dict = None):
    reminder_id = str(uuid.uuid4())[:12]
    ts = now_iso()
    conn = get_db()
    conn.execute(
        "INSERT INTO reminders (id, title, description, due_at, completed, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (reminder_id, title, description, due_at, 0, ts, json.dumps(metadata or {})),
    )
    conn.commit()
    conn.close()
    return {"id": reminder_id, "title": title, "due_at": due_at, "created_at": ts}


def list_reminders(completed: bool = None, limit: int = 50):
    conn = get_db()
    if completed is True:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE completed = 1 ORDER BY due_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    elif completed is False:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE completed = 0 ORDER BY due_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM reminders ORDER BY due_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "description": r["description"],
            "due_at": r["due_at"],
            "completed": bool(r["completed"]),
            "created_at": r["created_at"],
            "metadata": json.loads(r["metadata"] or "{}"),
        }
        for r in rows
    ]


def complete_reminder(reminder_id: str, completed: bool = True):
    conn = get_db()
    conn.execute(
        "UPDATE reminders SET completed = ? WHERE id = ?",
        (1 if completed else 0, reminder_id),
    )
    conn.commit()
    conn.close()


def delete_reminder(reminder_id: str):
    conn = get_db()
    conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()


def get_overdue_reminders():
    conn = get_db()
    now = now_iso()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE completed = 0 AND due_at < ? ORDER BY due_at ASC",
        (now,),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "description": r["description"],
            "due_at": r["due_at"],
            "completed": False,
            "created_at": r["created_at"],
            "metadata": json.loads(r["metadata"] or "{}"),
        }
        for r in rows
    ]


def get_reminders_today():
    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT * FROM reminders WHERE completed = 0 AND due_at >= ? AND due_at < ? ORDER BY due_at ASC",
        (f"{today}T00:00:00", f"{today}T23:59:59"),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "description": r["description"],
            "due_at": r["due_at"],
            "completed": False,
            "created_at": r["created_at"],
            "metadata": json.loads(r["metadata"] or "{}"),
        }
        for r in rows
    ]