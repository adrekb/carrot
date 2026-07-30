import json
import uuid
from datetime import datetime, timezone

from carrot.database import get_db


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_goal(title: str, description: str = "", category: str = "", metadata: dict = None):
    goal_id = str(uuid.uuid4())[:12]
    ts = now_iso()
    conn = get_db()
    conn.execute(
        "INSERT INTO goals (id, title, description, category, created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (goal_id, title, description, category, ts, ts, json.dumps(metadata or {})),
    )
    conn.commit()
    conn.close()
    return {"id": goal_id, "title": title, "category": category, "created_at": ts}


def get_goal(goal_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "category": row["category"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "metadata": json.loads(row["metadata"] or "{}"),
    }


def list_goals(category: str = None, limit: int = 50):
    conn = get_db()
    if category:
        rows = conn.execute(
            "SELECT * FROM goals WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
            (category, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM goals ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "description": r["description"],
            "category": r["category"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "metadata": json.loads(r["metadata"] or "{}"),
        }
        for r in rows
    ]


def add_data_point(goal_id: str, value, label: str = "", metadata: dict = None):
    conn = get_db()
    goal = conn.execute("SELECT metadata FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if goal is None:
        conn.close()
        raise ValueError(f"Goal {goal_id} not found")
    data = json.loads(goal["metadata"] or "{}")
    data_points = data.get("data_points", [])
    data_points.append(
        {
            "value": value,
            "label": label,
            "timestamp": now_iso(),
            "metadata": metadata or {},
        }
    )
    conn.execute(
        "UPDATE goals SET metadata = ?, updated_at = ? WHERE id = ?",
        (json.dumps(data), now_iso(), goal_id),
    )
    conn.commit()
    conn.close()
    return data_points


def get_goal_history(goal_id: str, start: str = None, end: str = None):
    conn = get_db()
    goal = conn.execute("SELECT metadata FROM goals WHERE id = ?", (goal_id,)).fetchone()
    conn.close()
    if goal is None:
        return []
    data = json.loads(goal["metadata"] or "{}")
    points = data.get("data_points", [])
    if start or end:
        filtered = []
        for p in points:
            ts = p.get("timestamp", "")
            if start and ts < start:
                continue
            if end and ts > end:
                continue
            filtered.append(p)
        return filtered
    return points