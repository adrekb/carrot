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
    # Through `_row_to_goal`, which knows about the columns a proposal added.
    # Building the dict by hand here is how `status` came back missing from
    # the one function everything else calls to read a goal.
    return _row_to_goal(row)


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
    return [_row_to_goal(r) for r in rows]


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

# ===== Proposals =====
#
# A goal used to be something you typed into a tab, which meant the only goals
# Carrot had were the ones you thought to enter twice — once by saying it and
# again by recording it. Chat proposes them now, so a goal has a state before
# it is a goal.
#
# `proposed`  — Carrot noticed something and is asking.
# `accepted`  — you ticked it. It is a goal and a memory.
# `declined`  — you dismissed it. Nothing is stored *and* the subject stops
#               being offered, because a proposal made again next week is
#               worse than never having made it.
# `done`      — you finished it.
STATUS_PROPOSED = "proposed"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"
STATUS_DONE = "done"


def _row_to_goal(row):
    keys = row.keys()

    def field(name, default=""):
        return row[name] if name in keys else default

    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "category": row["category"],
        "status": field("status", STATUS_ACCEPTED),
        "deadline": field("deadline"),
        "subject": field("subject"),
        "conversation_id": field("conversation_id"),
        "message_id": field("message_id"),
        "source_text": field("source_text"),
        "decided_at": field("decided_at"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "metadata": json.loads(row["metadata"] or "{}"),
    }


def propose(title: str, subject: str = "", deadline: str = "", target: str = "",
            conversation_id: str = "", message_id: str = "", source_text: str = ""):
    """Record a proposed goal. Not a goal yet — a question with a row behind it.

    Stored rather than held in the reply because the chip has to survive a
    reload: a conversation reopened tomorrow should still show what Carrot
    asked and let you answer it, and an unanswered question that evaporates on
    refresh is one the user never gets to decide.
    """
    goal_id = str(uuid.uuid4())[:12]
    ts = now_iso()
    conn = get_db()
    conn.execute(
        "INSERT INTO goals (id, title, description, category, status, deadline, subject,"
        " conversation_id, message_id, source_text, created_at, updated_at, metadata)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (goal_id, title, "", "", STATUS_PROPOSED, deadline, subject,
         conversation_id, message_id, source_text, ts, ts,
         json.dumps({"target": target} if target else {})),
    )
    conn.commit()
    conn.close()
    return get_goal(goal_id)


def decide(goal_id: str, accepted: bool):
    """Tick or dismiss a proposal.

    Accepting also writes a memory, because a goal you agreed to is a fact
    about you and belongs where the rest of them are — that is what makes it
    answerable in Cursor three months later rather than only in this tab.
    """
    goal = get_goal(goal_id)
    if goal is None or goal.get("status") != STATUS_PROPOSED:
        return None
    status = STATUS_ACCEPTED if accepted else STATUS_DECLINED
    ts = now_iso()
    conn = get_db()
    conn.execute(
        "UPDATE goals SET status = ?, decided_at = ?, updated_at = ? WHERE id = ?",
        (status, ts, ts, goal_id),
    )
    conn.commit()
    conn.close()
    if accepted:
        _remember(goal)
    return get_goal(goal_id)


def _remember(goal):
    """A goal, as a fact about the person. Never fatal — a memory that could
    not be written must not lose the goal that was agreed to."""
    try:
        from carrot import memory as memory_mod

        deadline = goal.get("deadline")
        content = goal["title"] + (f" (by {deadline})" if deadline else "")
        memory_mod.create(
            kind="project",
            subject=goal.get("subject") or goal["title"][:60],
            content=content,
            confidence=1.0,
            source_conversation_id=goal.get("conversation_id") or None,
            origin=memory_mod.ORIGIN_CHAT,
        )
    except Exception:
        pass


def declined_subjects() -> set:
    """What not to ask about again.

    The same shape as `memory._rejected_subjects`, and for the same reason: a
    system that proposes has to remember being told no, or it is not proposing,
    it is nagging.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT subject FROM goals WHERE status = ? AND subject != ''",
            (STATUS_DECLINED,),
        ).fetchall()
    except Exception:
        conn.close()
        return set()
    conn.close()
    return {r["subject"] for r in rows}


def by_status(status: str, limit: int = 50):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM goals WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
        (status, limit),
    ).fetchall()
    conn.close()
    return [_row_to_goal(r) for r in rows]


def proposals_for(conversation_id: str):
    """Undecided proposals from one conversation, so reopening it shows the
    question again rather than quietly dropping it."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM goals WHERE conversation_id = ? AND status = ?"
        " ORDER BY created_at",
        (conversation_id, STATUS_PROPOSED),
    ).fetchall()
    conn.close()
    return [_row_to_goal(r) for r in rows]
