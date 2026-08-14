"""Tasks the agent runs on a schedule, with nobody at the keyboard.

"Every morning, tell me what changed in the repo yesterday" is a question
somebody asks by opening the app and typing it, which means it gets asked on
the mornings they remember. The work is identical every time; the only reason
it needs a person is that nothing else was going to start it.

The whole design problem here is the second half of that sentence — *nobody
at the keyboard* — because every safety control this app has assumes someone
is there to answer it.

**An unattended run is read-only. Not by default — at all.** The approval
gate cannot be satisfied by a run nobody is watching: it blocks on a click
that is not coming and dies at the timeout having done half of something. A
flag to let one task write anyway would have to bypass that gate, and a
bypass is not something to add as a checkbox on a form for scheduling a
morning summary. So these tasks read, search and report, and the sentence in
the UI says so. "Fix the failing test at 3am" is a real thing to want and it
needs its own consent flow, not this one's spare field.

**A run that nobody watched has to leave a trace.** Every run stores its
output and raises a notification, because the alternative is an assistant
that did something at 4am and mentioned it to no one.

**A schedule cannot run twice for the same slot.** The tick is every minute
and the clock is the machine's, so "is it 09:00 yet" is asked sixty times an
hour; what stops sixty runs is that the answer is written down before the
work starts.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .database import get_db

# How the schedule is written down. Deliberately three shapes rather than a
# cron expression: "0 9 * * 1" is a thing people get wrong silently, and the
# three that were actually wanted are hourly, daily and weekly.
EVERY_HOUR = "hourly"
EVERY_DAY = "daily"
EVERY_WEEK = "weekly"
SCHEDULES = (EVERY_HOUR, EVERY_DAY, EVERY_WEEK)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")

# A scheduled run gets a wide berth but not an unlimited one: something stuck
# at 4am must not still be stuck at 4pm, holding a provider connection and a
# workspace lock.
MAX_RUN_SECONDS = 900
# More rounds than a parallel investigator gets: this is the whole task, not
# one quarter of one, and nobody is waiting on the screen for it.
SCHEDULED_ROUNDS = 8


def _now() -> datetime:
    return datetime.now()


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


# ===== Storage =====

def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY,
            prompt TEXT NOT NULL,
            schedule TEXT NOT NULL,
            at TEXT DEFAULT '09:00',
            weekday TEXT DEFAULT 'monday',
            enabled INTEGER DEFAULT 1,
            last_run TEXT DEFAULT '',
            last_status TEXT DEFAULT '',
            last_output TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )""")


def _row(row) -> Dict[str, Any]:
    task = dict(row)
    task["enabled"] = bool(task["enabled"])
    return task


def create(prompt: str, schedule: str = EVERY_DAY, at: str = "09:00",
           weekday: str = "monday") -> Dict[str, Any]:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("a scheduled task needs something to do")
    if schedule not in SCHEDULES:
        schedule = EVERY_DAY
    task_id = str(uuid.uuid4())[:12]
    conn = get_db()
    _ensure_table(conn)
    conn.execute(
        """INSERT INTO scheduled_tasks
           (id, prompt, schedule, at, weekday, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, 1, ?)""",
        (task_id, prompt, schedule, _valid_time(at), weekday.lower(), _iso(_now())),
    )
    conn.commit()
    conn.close()
    return get(task_id)


def _valid_time(at: str) -> str:
    """``HH:MM``, or 09:00. A bad time silently meaning midnight is how a
    task nobody scheduled for 00:00 starts running at 00:00."""
    try:
        hour, minute = (int(part) for part in str(at).split(":")[:2])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    except (ValueError, TypeError):
        pass
    return "09:00"


def get(task_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    _ensure_table(conn)
    row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return _row(row) if row else None


def list_tasks() -> List[Dict[str, Any]]:
    conn = get_db()
    _ensure_table(conn)
    rows = conn.execute("SELECT * FROM scheduled_tasks ORDER BY created_at").fetchall()
    conn.close()
    return [_row(row) for row in rows]


def update(task_id: str, **fields) -> Optional[Dict[str, Any]]:
    allowed = {"prompt", "schedule", "at", "weekday", "enabled"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "at":
            value = _valid_time(value)
        if key == "enabled":
            value = 1 if value else 0
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return get(task_id)
    conn = get_db()
    _ensure_table(conn)
    conn.execute(f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE id = ?",
                 (*values, task_id))
    conn.commit()
    conn.close()
    return get(task_id)


def delete(task_id: str) -> bool:
    conn = get_db()
    _ensure_table(conn)
    cursor = conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


# ===== When it is due =====

def is_due(task: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Whether this task's slot has come round and has not been run for.

    Compared against the slot rather than against an elapsed interval: a
    machine that was asleep at 09:00 and woke at 11:00 should run the morning
    task once, late, not decide it is 0 hours into a new period and skip the
    day. The same comparison is what stops it running sixty times between
    09:00 and 09:59.
    """
    if not task.get("enabled"):
        return False
    now = now or _now()
    slot = _slot_for(task, now)
    if slot is None:
        return False
    last = task.get("last_run") or ""
    return last < slot


def _slot_for(task: Dict[str, Any], now: datetime) -> Optional[str]:
    """The identifier of the slot that is currently open, or None.

    A slot opens at its time and stays open until the next one, so a run
    missed by an hour still happens. It does not stay open forever: a laptop
    closed for a week should not wake up owing seven runs of the same task.
    """
    schedule = task.get("schedule")
    if schedule == EVERY_HOUR:
        return now.strftime("%Y-%m-%dT%H")

    at = _valid_time(task.get("at", "09:00"))
    hour, minute = (int(part) for part in at.split(":"))

    if schedule == EVERY_DAY:
        if (now.hour, now.minute) < (hour, minute):
            return None
        return f"{now.strftime('%Y-%m-%d')}T{at}"

    if schedule == EVERY_WEEK:
        wanted = task.get("weekday", "monday").lower()
        if wanted not in WEEKDAYS:
            wanted = "monday"
        if WEEKDAYS[now.weekday()] != wanted:
            return None
        if (now.hour, now.minute) < (hour, minute):
            return None
        return f"{now.strftime('%Y-%m-%d')}T{at}"

    return None


# ===== Running one =====

def run_task(task: Dict[str, Any], runner=None,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """Run one task and record what happened.

    The slot is written down *before* the work starts. The tick asks "is it
    09:00 yet" sixty times an hour, and what stops sixty runs is that the
    first one has already claimed the slot — not that the work finishes fast
    enough to beat the next tick, which is not something a model call can
    promise.

    Pressing "run it now" outside the slot records the time and claims
    nothing, so the 09:00 run still happens: trying a task at midnight to see
    what it does is not the same as it having run for the morning, and having
    it silently eat the appointment would be a trap.

    ``now`` is injectable because the alternative is a test whose result
    depends on what time it is when you run it — which is how this arrived,
    passing all evening and failing at two in the morning.
    """
    from . import proactive

    now = now or _now()
    slot = _slot_for(task, now) or _iso(now)
    _claim(task["id"], slot)

    try:
        output = (runner or _run_through_the_agent)(task)
        status = "ok"
    except Exception as exc:
        output, status = f"{exc}", "failed"

    conn = get_db()
    _ensure_table(conn)
    conn.execute(
        "UPDATE scheduled_tasks SET last_status = ?, last_output = ? WHERE id = ?",
        (status, str(output)[:8000], task["id"]),
    )
    conn.commit()
    conn.close()

    # A run nobody watched has to leave a trace, or this is an assistant that
    # did something at 4am and mentioned it to no one.
    proactive.create(
        kind="scheduled_task",
        title=("Scheduled task finished" if status == "ok" else "Scheduled task failed"),
        body=f"{task['prompt']}\n\n{str(output)[:1500]}",
        severity="info" if status == "ok" else "warning",
        metadata={"task_id": task["id"], "status": status},
    )
    return {"status": status, "output": output}


def _claim(task_id: str, slot: str) -> None:
    conn = get_db()
    _ensure_table(conn)
    conn.execute("UPDATE scheduled_tasks SET last_run = ? WHERE id = ?", (slot, task_id))
    conn.commit()
    conn.close()


def _run_through_the_agent(task: Dict[str, Any]) -> str:
    """One read-only agent, on the same tools the parallel investigators get.

    Deliberately not the Code tab's full pipeline. That one decides what it
    may do from the global `coder_mode` setting, so running it here would mean
    writing to that setting — flipping the user's own Plan/Act switch at 4am,
    and leaving it flipped if the process died mid-run. Sharing the read-only
    runner instead means the restriction is a property of how this executes
    rather than of a setting anything else can change underneath it.
    """
    from . import subagents

    run_tool, tools = subagents.read_only_runner()
    return subagents.run_one(
        name="scheduled", task=task["prompt"], run_tool=run_tool, tools=tools,
        emit=lambda event: None, rounds=SCHEDULED_ROUNDS,
        # The limit this module has always claimed and briefly stopped
        # enforcing: swapping the runner took the deadline out with it, and
        # left the constant sitting here describing a guarantee nothing kept.
        deadline=time.monotonic() + MAX_RUN_SECONDS,
        context_note="You are running on a schedule. Nobody is at the keyboard, "
                     "so report what you find rather than asking a question.",
    )


# ===== The tick =====

_started = False


def check_due(now: Optional[datetime] = None, runner=None) -> List[str]:
    """Run everything that is due. Returns the ids it ran.

    ``runner`` is threaded through rather than patched over: the loop catches
    every exception on purpose, so a test that replaces ``run_task`` from the
    outside has its own mistakes swallowed here and reports a passing silence.
    """
    ran = []
    for task in list_tasks():
        try:
            if is_due(task, now):
                run_task(task, runner=runner, now=now)
                ran.append(task["id"])
        except Exception:
            # One task failing is not the scheduler failing. Its own row
            # records the failure; the loop keeps its other appointments.
            continue
    return ran


def _loop():
    while True:
        try:
            check_due()
        except Exception:
            pass
        time.sleep(60)


def start_scheduler():
    """Idempotent, like the overnight recap's — the app can call it twice."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="carrot-scheduled-tasks").start()
