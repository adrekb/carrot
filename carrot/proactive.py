"""Proactive notifications.

Carrot only ever spoke when spoken to, which makes it useless for the things it
is best placed to notice: a reminder that quietly went overdue, a goal nobody
has logged in three weeks, an assignment due tonight. A background watcher runs
the checks on an interval and raises notifications the UI and the Electron shell
surface as native toasts.

Every check produces a stable ``dedupe_key``, so a reminder that stays overdue
for a week generates one notification rather than a hundred.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from .config import get_config
from .database import get_db

CHECK_INTERVAL_SECONDS = 300
GOAL_STALE_DAYS = 14
REMINDER_SOON_HOURS = 6

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_URGENT = "urgent"

_watcher: Optional[threading.Thread] = None
_stop = threading.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ===== Store =====

def create(
    kind: str,
    title: str,
    body: str = "",
    severity: str = SEVERITY_INFO,
    dedupe_key: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Raise a notification, or return None when one already exists for the key.

    Dedupe looks only at live (non-dismissed) notifications, so once the user
    clears one, a recurrence can legitimately raise it again.
    """
    conn = get_db()
    if dedupe_key:
        existing = conn.execute(
            "SELECT id FROM notifications WHERE dedupe_key = ? AND dismissed = 0",
            (dedupe_key,),
        ).fetchone()
        if existing:
            conn.close()
            return None

    notification_id = str(uuid.uuid4())[:12]
    conn.execute(
        """INSERT INTO notifications
           (id, kind, title, body, severity, dedupe_key, read, dismissed, created_at, metadata)
           VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)""",
        (
            notification_id, kind, title, body, severity, dedupe_key,
            _now_iso(), json.dumps(metadata or {}),
        ),
    )
    conn.commit()
    conn.close()
    return get(notification_id)


def _row_to_notification(row) -> Dict[str, Any]:
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "severity": row["severity"],
        "read": bool(row["read"]),
        "dismissed": bool(row["dismissed"]),
        "created_at": row["created_at"],
        "metadata": metadata,
    }


def get(notification_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    conn.close()
    return _row_to_notification(row) if row else None


def list_notifications(unread_only: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
    query = "SELECT * FROM notifications WHERE dismissed = 0"
    if unread_only:
        query += " AND read = 0"
    query += " ORDER BY created_at DESC LIMIT ?"
    conn = get_db()
    rows = conn.execute(query, (limit,)).fetchall()
    conn.close()
    return [_row_to_notification(r) for r in rows]


def unread_count() -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE read = 0 AND dismissed = 0"
    ).fetchone()
    conn.close()
    return row["c"]


def mark_read(notification_id: str) -> bool:
    conn = get_db()
    cursor = conn.execute("UPDATE notifications SET read = 1 WHERE id = ?", (notification_id,))
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def mark_all_read() -> int:
    conn = get_db()
    cursor = conn.execute("UPDATE notifications SET read = 1 WHERE read = 0 AND dismissed = 0")
    conn.commit()
    count = cursor.rowcount
    conn.close()
    return count


def dismiss_by_key(dedupe_key: str) -> bool:
    """Dismiss whatever is live under a dedupe key.

    Used for notifications that describe a *pending* state rather than an
    event: once an approval is answered, the alert about it is not history, it
    is noise, and the raiser knows the key rather than the id.
    """
    if not dedupe_key:
        return False
    conn = get_db()
    cursor = conn.execute(
        "UPDATE notifications SET dismissed = 1, read = 1 WHERE dedupe_key = ? AND dismissed = 0",
        (dedupe_key,),
    )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def dismiss(notification_id: str) -> bool:
    """Dismiss a notification. False when it is unknown or already dismissed."""
    conn = get_db()
    cursor = conn.execute(
        "UPDATE notifications SET dismissed = 1, read = 1 WHERE id = ? AND dismissed = 0",
        (notification_id,),
    )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


# ===== Checks =====

def check_overdue_reminders() -> List[Dict[str, Any]]:
    from . import reminders as reminders_mod

    raised = []
    for reminder in reminders_mod.get_overdue_reminders():
        notification = create(
            kind="reminder_overdue",
            title=f"Overdue: {reminder['title']}",
            body=reminder.get("description") or f"Was due {reminder.get('due_at', '')[:16]}",
            severity=SEVERITY_URGENT,
            dedupe_key=f"reminder_overdue:{reminder['id']}",
            metadata={"reminder_id": reminder["id"]},
        )
        if notification:
            raised.append(notification)
    return raised


def check_upcoming_reminders() -> List[Dict[str, Any]]:
    """Warn about reminders coming due inside the next few hours."""
    from . import reminders as reminders_mod

    cutoff = _now() + timedelta(hours=REMINDER_SOON_HOURS)
    raised = []
    for reminder in reminders_mod.list_reminders(completed=False, limit=200):
        due_at = reminder.get("due_at")
        if not due_at:
            continue
        try:
            due = datetime.fromisoformat(due_at)
        except ValueError:
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if not (_now() < due <= cutoff):
            continue
        notification = create(
            kind="reminder_soon",
            title=f"Due soon: {reminder['title']}",
            body=f"Due at {due_at[:16]}",
            severity=SEVERITY_WARNING,
            dedupe_key=f"reminder_soon:{reminder['id']}",
            metadata={"reminder_id": reminder["id"]},
        )
        if notification:
            raised.append(notification)
    return raised


def check_stale_goals() -> List[Dict[str, Any]]:
    """Nudge on goals with no logged progress for a while."""
    from . import goals as goals_mod

    cutoff = _now() - timedelta(days=GOAL_STALE_DAYS)
    raised = []
    for goal in goals_mod.list_goals(limit=100):
        history = goals_mod.get_goal_history(goal["id"])
        if history:
            last = history[-1].get("timestamp") or history[-1].get("created_at") or ""
            try:
                last_at = datetime.fromisoformat(last)
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=timezone.utc)
                if last_at > cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            days = (_now() - last_at).days
            body = f"No progress logged in {days} days."
        else:
            try:
                created = datetime.fromisoformat(goal["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created > cutoff:
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            body = "No progress has ever been logged for this goal."

        # Week-stamped so a goal that stays stale nudges occasionally, not daily.
        week = _now().strftime("%Y-W%W")
        notification = create(
            kind="goal_stale",
            title=f"Goal going quiet: {goal['title']}",
            body=body,
            severity=SEVERITY_INFO,
            dedupe_key=f"goal_stale:{goal['id']}:{week}",
            metadata={"goal_id": goal["id"]},
        )
        if notification:
            raised.append(notification)
    return raised


def check_assignments_due() -> List[Dict[str, Any]]:
    from . import computer_use as computer_use_mod

    raised = []
    try:
        assignments = computer_use_mod.find_assignments() or []
    except Exception:
        return raised

    for assignment in assignments[:50]:
        due_at = assignment.get("due_date") or assignment.get("due_at")
        if not due_at:
            continue
        try:
            due = datetime.fromisoformat(str(due_at))
        except ValueError:
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if not (_now() < due <= _now() + timedelta(days=2)):
            continue
        name = assignment.get("name") or assignment.get("title") or "Assignment"
        notification = create(
            kind="assignment_due",
            title=f"Assignment due: {name}",
            body=f"Due {str(due_at)[:16]}",
            severity=SEVERITY_WARNING,
            dedupe_key=f"assignment:{name}:{str(due_at)[:10]}",
            metadata={"assignment": name},
        )
        if notification:
            raised.append(notification)
    return raised


def check_embedding_backlog() -> List[Dict[str, Any]]:
    """Flag a large unembedded backlog — semantic recall is degraded until it clears."""
    from . import vectors as vectors_mod

    pending = sum(vectors_mod.pending(ns) for ns in vectors_mod.BACKFILL_SOURCES)
    if pending < 500:
        return []
    notification = create(
        kind="embedding_backlog",
        title=f"{pending} items are waiting to be embedded",
        body="Semantic search will miss them until the backfill runs. Start it from Settings.",
        severity=SEVERITY_INFO,
        dedupe_key=f"embedding_backlog:{_now().strftime('%Y-%m-%d')}",
        metadata={"pending": pending},
    )
    return [notification] if notification else []


CHECKS: Dict[str, Callable[[], List[Dict[str, Any]]]] = {
    "reminders_overdue": check_overdue_reminders,
    "reminders_soon": check_upcoming_reminders,
    "goals_stale": check_stale_goals,
    "assignments_due": check_assignments_due,
    "embedding_backlog": check_embedding_backlog,
}


def run_checks(names: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run the enabled checks. One failing check never stops the others."""
    disabled = set(get_config().get("proactive_disabled_checks", []) or [])
    selected = names or [n for n in CHECKS if n not in disabled]

    raised, errors = [], {}
    for name in selected:
        check = CHECKS.get(name)
        if check is None:
            continue
        try:
            raised.extend(check())
        except Exception as exc:
            errors[name] = str(exc)
    return {
        "checked": selected,
        "raised": raised,
        "count": len(raised),
        "errors": errors,
        "ran_at": _now_iso(),
    }


# ===== Watcher =====

def enabled() -> bool:
    return bool(get_config().get("proactive_enabled", True))


def _watch_loop():
    while not _stop.is_set():
        try:
            if enabled():
                run_checks()
        except Exception:
            pass
        # Reading the interval is a database call too. Unguarded, a database
        # that disappeared under the watcher took the thread down with it —
        # which meant proactive checks stopped for the rest of the session and
        # nothing said so.
        try:
            interval = int(get_config().get("proactive_interval_seconds", CHECK_INTERVAL_SECONDS))
        except Exception:
            interval = CHECK_INTERVAL_SECONDS
        _stop.wait(max(60, interval))


def start_watcher():
    """Start the background watcher (idempotent).

    The clear comes first. It used to sit after the liveness check, which is a
    race: a thread that has been asked to stop is still alive until it next
    looks at the flag, so a start arriving in that window returned early —
    leaving the flag set — and the thread then exited on it. The result was no
    watcher at all, from a call whose whole job was to guarantee one. Clearing
    first means a pending stop is cancelled either way.
    """
    global _watcher
    _stop.clear()
    if _watcher is not None and _watcher.is_alive():
        return
    _watcher = threading.Thread(target=_watch_loop, daemon=True, name="carrot-proactive")
    _watcher.start()


def stop_watcher():
    _stop.set()


def stream_new(poll_seconds: float = 2.0, timeout_seconds: float = 300.0):
    """Yield notifications as they appear, for the UI's SSE connection.

    Starts from the newest existing notification so a reconnecting client is not
    re-shown everything it has already seen.
    """
    seen_since = _now_iso()
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        conn = get_db()
        rows = conn.execute(
            """SELECT * FROM notifications
               WHERE created_at > ? AND dismissed = 0
               ORDER BY created_at ASC""",
            (seen_since,),
        ).fetchall()
        conn.close()
        for row in rows:
            seen_since = row["created_at"]
            yield _row_to_notification(row)
        time.sleep(poll_seconds)
