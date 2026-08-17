"""What Carrot is doing, and what you were last doing.

The nav rail was four buttons and six hundred pixels of nothing. That is not
restraint, it is a gap where the two questions a person actually has while
using the app should be answered:

  1. *Is anything still running?* Carrot starts work that outlives the screen
     you started it on — an agent run, a deep research run, an index scan. Once
     you navigate away there is no longer anywhere that says so. A job you
     cannot see is a job you assume died, and the fix people reach for is to
     start it again.

  2. *What was I just doing?* Answered today only by opening a tab and looking.

The two are deliberately unequal. Running work is the thing worth interrupting
someone for, so it is at the top and it is absent — not empty, absent — when
nothing is running; a permanent "No running jobs" panel is a box that is wrong
about being useful most of the time. Recents sit underneath, collapsed, because
they are a thing you go looking for rather than a thing that should be shouting
while you work.

One endpoint rather than four. The rail is a single strip of UI and it should
cost a single request; three separate polls for three kinds of job is how a
sidebar becomes the reason the app feels busy.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from carrot.database import get_db

# A run whose row still says "running" long after anything plausibly could be.
#
# Nothing reconciles these tables on startup: kill Carrot mid-agent-run and the
# row keeps `status='running'` for ever. Left alone, the rail would then show a
# job that is not running, permanently, and the one panel whose whole job is to
# tell you what is live would be lying — which is worse than not having it.
#
# So a run is only reported as live if it started after this process did. That
# is exact rather than a heuristic: the work happens in this process, so a run
# from before this process began cannot still be running in it, whatever its
# row says. Those are reported as `interrupted`, which is what they are, and
# is also the first time anything in Carrot has said so.
_PROCESS_STARTED = datetime.now(timezone.utc)

# How many finished things the collapsed list holds. Small on purpose: this is
# "what was I just doing", not a history browser. The history menu and Ctrl+K
# are both one gesture away and both search properly.
RECENT_LIMIT = 6


def _parse(stamp: Optional[str]) -> Optional[datetime]:
    """An ISO timestamp from the database, or None if it is unreadable.

    Rows are written by us and are always ISO, but a value that fails to parse
    must not take out the whole rail — the caller treats None as "cannot tell",
    which lands on the safe side of every decision here.
    """
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    # Rows written before timezone handling was consistent come back naive.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_live(started: Optional[str]) -> bool:
    when = _parse(started)
    return when is not None and when >= _PROCESS_STARTED


def _truncate(text: str, limit: int = 70) -> str:
    """A task or question short enough for a 220px rail.

    Cut on a word so the label reads as a shortened sentence rather than as a
    string that ran out.
    """
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _job_rows(sql: str, kind: str, label_column: str) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()
    out = []
    for row in rows:
        out.append({
            "kind": kind,
            "id": row["id"],
            "label": _truncate(row.get(label_column) or ""),
            "status": row["status"],
            "started_at": row.get("created_at"),
            "finished_at": row.get("finished_at"),
            "detail": row,
        })
    return out


def running() -> List[Dict[str, Any]]:
    """Everything genuinely still working, newest first.

    Rows claiming to run that predate this process are reported as
    ``interrupted`` rather than dropped: a research run that died halfway is
    something the user is owed an explanation for, and silently hiding it is
    how it becomes "Carrot lost my report".
    """
    jobs: List[Dict[str, Any]] = []

    for job in _job_rows(
        "SELECT id, task, status, surface, steps_used, created_at, finished_at "
        "FROM agent_runs WHERE status = 'running' ORDER BY created_at DESC",
        "agent", "task",
    ):
        detail = job.pop("detail")
        job["status"] = "running" if _is_live(job["started_at"]) else "interrupted"
        job["progress"] = f"step {detail['steps_used']}" if detail.get("steps_used") else ""
        job["where"] = detail.get("surface") or ""
        jobs.append(job)

    for job in _job_rows(
        "SELECT id, question, status, depth, created_at, finished_at "
        "FROM research_runs WHERE status = 'running' ORDER BY created_at DESC",
        "research", "question",
    ):
        detail = job.pop("detail")
        job["status"] = "running" if _is_live(job["started_at"]) else "interrupted"
        job["progress"] = detail.get("depth") or ""
        job["where"] = ""
        jobs.append(job)

    # The index scan is in memory rather than in a table, and it is the one
    # job here with a real denominator, so it is the one that can show a count
    # instead of a spinner.
    try:
        from carrot import indexer

        scan = indexer.scan_state()
        if scan.get("running"):
            jobs.append({
                "kind": "index",
                "id": "index-scan",
                "label": _truncate(scan.get("current") or "Indexing documents"),
                "status": "running",
                "started_at": scan.get("started_at"),
                "finished_at": None,
                "progress": f"{scan.get('indexed', 0)} indexed"
                            + (f", {scan['failed']} failed" if scan.get("failed") else ""),
                "where": "",
            })
    except Exception:
        # An index module that cannot answer must not empty the rail of the
        # agent and research jobs that answered fine.
        pass

    jobs.sort(key=lambda j: j.get("started_at") or "", reverse=True)
    return jobs


def recent(limit: int = RECENT_LIMIT) -> List[Dict[str, Any]]:
    """What you were last doing — conversations, plus finished runs.

    Conversations dominate because that is what the app mostly is; runs are
    included because "the research I kicked off before lunch" is exactly the
    thing people come back looking for and it is not a conversation.
    """
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, updated_at FROM conversations "
            "WHERE COALESCE(title, '') != '' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()]
    finally:
        conn.close()
    items = [{
        "kind": "conversation",
        "id": row["id"],
        "label": _truncate(row["title"], 46),
        "at": row["updated_at"],
    } for row in rows]

    for sql, kind, column in (
        ("SELECT id, question AS label, finished_at FROM research_runs "
         "WHERE status != 'running' AND finished_at IS NOT NULL "
         "ORDER BY finished_at DESC LIMIT 3", "research", "label"),
        ("SELECT id, task AS label, finished_at FROM agent_runs "
         "WHERE status != 'running' AND finished_at IS NOT NULL "
         "ORDER BY finished_at DESC LIMIT 3", "agent", "label"),
    ):
        conn = get_db()
        try:
            rows = [dict(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()
        items += [{"kind": kind, "id": r["id"], "label": _truncate(r["label"], 46),
                   "at": r["finished_at"]} for r in rows]

    items.sort(key=lambda i: i.get("at") or "", reverse=True)
    return items[:limit]


def overview(limit: int = RECENT_LIMIT) -> Dict[str, Any]:
    """Everything the rail draws, in one request.

    Never raises: this is polled, and a rail that throws is a console full of
    errors and a strip of UI that vanishes mid-session. A half-answer is a
    better rail than an exception.
    """
    jobs: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []
    try:
        jobs = running()
    except Exception:
        pass
    try:
        items = recent(limit)
    except Exception:
        pass
    return {
        "running": jobs,
        "recent": items,
        # So the client can slow its polling down when there is nothing to
        # watch, rather than asking every two seconds for ever.
        "any_running": any(j["status"] == "running" for j in jobs),
    }
