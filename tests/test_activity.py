"""The nav rail's activity feed: what is running, and what you were last doing.

The interesting behaviour here is not the aggregation, it is the honesty of it.
Nothing reconciles `agent_runs` or `research_runs` on startup, so killing Carrot
mid-run leaves a row saying `status='running'` for ever. A rail that believed
those rows would be a panel whose entire purpose is saying what is live, being
permanently wrong — which is worse than not shipping the panel.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from carrot import activity
from carrot.database import get_db


def _iso(when: datetime) -> str:
    return when.isoformat()


def _add_agent_run(status: str, created: datetime, task: str = "Rename the columns",
                   finished: datetime = None, steps: int = 0):
    conn = get_db()
    conn.execute(
        """INSERT INTO agent_runs (id, task, status, surface, steps_used, created_at, finished_at)
           VALUES (?, ?, ?, 'browser', ?, ?, ?)""",
        (f"agent-{created.timestamp()}-{task[:6]}", task, status, steps,
         _iso(created), _iso(finished) if finished else None),
    )
    conn.commit()
    conn.close()


def _add_research_run(status: str, created: datetime, question: str = "Vulkan support",
                      finished: datetime = None):
    conn = get_db()
    conn.execute(
        """INSERT INTO research_runs (id, question, status, depth, created_at, finished_at)
           VALUES (?, ?, ?, 'standard', ?, ?)""",
        (f"res-{created.timestamp()}-{question[:6]}", question, status,
         _iso(created), _iso(finished) if finished else None),
    )
    conn.commit()
    conn.close()


class TestWhatIsActuallyRunning:
    def test_a_run_started_since_boot_is_live(self, isolated_db):
        _add_agent_run("running", datetime.now(timezone.utc), steps=4)
        jobs = activity.running()
        assert [j["status"] for j in jobs] == ["running"]
        assert jobs[0]["progress"] == "step 4"

    def test_a_run_predating_this_process_is_not_running(self, isolated_db):
        """The row says `running`. The process that would have been running it
        is gone. Reported as what it is rather than as live work, because a
        permanently-wrong "Running now" teaches people to stop reading it."""
        _add_research_run("running", datetime.now(timezone.utc) - timedelta(days=5))
        jobs = activity.running()
        assert [j["status"] for j in jobs] == ["interrupted"]

    def test_an_interrupted_run_is_not_hidden(self, isolated_db):
        """Dropping it would be tidier and worse: a research run that died
        halfway is the thing the user comes back for, and silently omitting it
        is how it becomes "Carrot lost my report"."""
        _add_research_run("running", datetime.now(timezone.utc) - timedelta(days=5),
                          question="F-35 status")
        assert any(j["label"] == "F-35 status" for j in activity.running())

    def test_any_running_is_false_when_only_wreckage_remains(self, isolated_db):
        """The client polls four times a minute while something is live. An
        interrupted row must not hold it at that rate for ever."""
        _add_agent_run("running", datetime.now(timezone.utc) - timedelta(days=2))
        data = activity.overview()
        assert data["running"] and data["any_running"] is False

    def test_finished_runs_are_not_reported_as_running(self, isolated_db):
        now = datetime.now(timezone.utc)
        _add_agent_run("done", now, finished=now)
        _add_research_run("done", now, finished=now)
        assert activity.running() == []


class TestRecents:
    def test_recent_mixes_conversations_and_finished_runs_newest_first(self, isolated_db):
        now = datetime.now(timezone.utc)
        conn = get_db()
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            ("c1", "an older chat", _iso(now - timedelta(hours=3)), _iso(now - timedelta(hours=3))),
        )
        conn.commit()
        conn.close()
        _add_research_run("done", now - timedelta(hours=2), question="the newer run",
                          finished=now - timedelta(minutes=1))

        items = activity.recent()
        assert [i["label"] for i in items] == ["the newer run", "an older chat"]

    def test_untitled_conversations_stay_out(self, isolated_db):
        """A conversation with no title renders as a blank row that opens
        something. There is nothing for the user to recognise it by."""
        now = _iso(datetime.now(timezone.utc))
        conn = get_db()
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            ("blank", "", now, now))
        conn.commit()
        conn.close()
        assert activity.recent() == []


class TestItNeverTakesTheRailDown:
    def test_overview_survives_a_broken_half(self, isolated_db, monkeypatch):
        """Polled every few seconds. An exception here is a console full of
        errors and a strip of UI that vanishes mid-session."""
        monkeypatch.setattr(activity, "running", lambda: 1 / 0)
        data = activity.overview()
        assert data == {"running": [], "recent": [], "any_running": False}

    def test_a_label_is_cut_to_fit_the_rail(self):
        long = "Rename every exported column in the quarterly sheet and re-file it under the new scheme"
        cut = activity._truncate(long, 40)
        assert len(cut) <= 41 and cut.endswith("…")
        # Cut on a word, so it reads as shortened rather than as truncated.
        assert not cut[:-1].endswith(" ")
        assert long.startswith(cut[:-1])

    def test_an_unparseable_timestamp_is_not_treated_as_live(self):
        assert activity._is_live("not a date") is False
        assert activity._is_live(None) is False


class TestTheEndpoint:
    def test_activity_endpoint_answers(self, client, isolated_db):
        body = client.get("/api/activity").json()
        assert set(body) == {"running", "recent", "any_running"}
        assert isinstance(body["running"], list)
