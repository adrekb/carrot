"""Work the agent does with nobody at the keyboard.

"Every morning, tell me what changed in the repo yesterday" is a question you
ask by opening the app and typing it, so it gets asked on the mornings you
remember. The work is the same every time; the only reason it needed a person
was that nothing else was going to start it.

The tests are mostly about the second half of "with nobody at the keyboard",
because every safety control in this app assumes somebody is there: the
approval gate blocks on a click that is not coming, and a run that did
something at 4am and told no one is worse than one that did not run.
"""
from datetime import datetime

import pytest

from carrot import scheduled


@pytest.fixture
def task(isolated_db):
    return scheduled.create("summarise yesterday's commits", schedule="daily", at="09:00")


class TestWhenItIsDue:
    def test_not_before_its_time(self, task):
        assert scheduled.is_due(task, datetime(2026, 8, 13, 8, 59)) is False

    def test_once_its_time_has_come(self, task):
        assert scheduled.is_due(task, datetime(2026, 8, 13, 9, 0)) is True

    def test_a_run_late_is_still_a_run(self, task):
        """A laptop asleep at 09:00 and awake at 11:00 should do the morning
        task once, late — not decide the window closed and skip the day."""
        assert scheduled.is_due(task, datetime(2026, 8, 13, 11, 30)) is True

    def test_not_sixty_times_in_the_hour_it_is_due(self, task):
        """The tick asks every minute. What stops sixty runs is that the slot
        is written down, not that the work finishes before the next tick."""
        scheduled.run_task(task, runner=lambda t: "done")
        after = scheduled.get(task["id"])
        for minute in range(0, 60, 7):
            assert scheduled.is_due(after, datetime.now().replace(hour=9, minute=minute)) is False

    def test_tomorrow_is_a_new_slot(self, isolated_db):
        daily = scheduled.create("a daily thing", schedule="daily", at="09:00")
        scheduled.update(daily["id"], **{})
        scheduled._claim(daily["id"], "2026-08-12T09:00")
        assert scheduled.is_due(scheduled.get(daily["id"]),
                                datetime(2026, 8, 13, 9, 0)) is True

    def test_a_disabled_task_is_never_due(self, task):
        scheduled.update(task["id"], enabled=False)
        assert scheduled.is_due(scheduled.get(task["id"]), datetime(2026, 8, 13, 9, 0)) is False

    def test_weekly_runs_on_its_day_and_no_other(self, isolated_db):
        weekly = scheduled.create("a weekly thing", schedule="weekly",
                                  at="09:00", weekday="monday")
        assert scheduled.is_due(weekly, datetime(2026, 8, 10, 9, 30)) is True    # Monday
        assert scheduled.is_due(weekly, datetime(2026, 8, 11, 9, 30)) is False   # Tuesday

    def test_hourly_is_due_every_hour(self, isolated_db):
        hourly = scheduled.create("an hourly thing", schedule="hourly")
        assert scheduled.is_due(hourly, datetime(2026, 8, 13, 3, 0)) is True

    def test_a_nonsense_time_does_not_silently_become_midnight(self, isolated_db):
        """A task nobody scheduled for 00:00 must not start running at 00:00."""
        odd = scheduled.create("x", schedule="daily", at="not a time")
        assert odd["at"] == "09:00"
        assert scheduled.create("y", schedule="daily", at="99:99")["at"] == "09:00"


class TestRunningOne:
    def test_the_slot_is_claimed_before_the_work_starts(self, task):
        """Not after. A model call takes longer than a minute often enough
        that 'it finishes before the next tick' is not a guarantee."""
        seen = {}

        def slow(t):
            seen["claimed"] = scheduled.get(t["id"])["last_run"]
            return "done"

        scheduled.run_task(task, runner=slow)
        assert seen["claimed"], "the slot was still empty while the task ran"

    def test_what_it_produced_is_kept(self, task):
        scheduled.run_task(task, runner=lambda t: "three commits, all in the parser")
        assert "three commits" in scheduled.get(task["id"])["last_output"]
        assert scheduled.get(task["id"])["last_status"] == "ok"

    def test_a_run_that_failed_says_so_rather_than_vanishing(self, task):
        def boom(t):
            raise RuntimeError("the provider was down")

        scheduled.run_task(task, runner=boom)
        stored = scheduled.get(task["id"])
        assert stored["last_status"] == "failed"
        assert "provider was down" in stored["last_output"]

    def test_every_run_leaves_a_notification(self, task):
        """The alternative is an assistant that did something at 4am and
        mentioned it to nobody."""
        from carrot import proactive

        scheduled.run_task(task, runner=lambda t: "all quiet")
        titles = [n["title"] for n in proactive.list_notifications()]
        assert any("Scheduled task" in title for title in titles)

    def test_one_task_failing_does_not_stop_the_others(self, isolated_db):
        """The loop keeps its other appointments. Each task's own row records
        what happened to it."""
        scheduled.create("first", schedule="hourly")
        scheduled.create("second", schedule="hourly")
        calls = []

        def flaky(t):
            calls.append(t["prompt"])
            if t["prompt"] == "first":
                raise RuntimeError("nope")
            return "fine"

        ran = scheduled.check_due(runner=flaky)
        assert calls == ["first", "second"]
        assert len(ran) == 2
        states = {t["prompt"]: t["last_status"] for t in scheduled.list_tasks()}
        assert states == {"first": "failed", "second": "ok"}


class TestWhatAnUnattendedRunMayDo:
    def test_it_cannot_change_anything(self):
        """Not a default — at all. The approval gate blocks on a click that is
        not coming, so a run that needs one dies at the timeout having done
        half of something."""
        from carrot import subagents

        run_tool, _ = subagents.read_only_runner()
        for forbidden in ("write_file", "edit_file", "run_command", "git_commit"):
            assert "not available" in run_tool(forbidden, {})

    def test_it_does_not_touch_the_users_plan_act_switch(self):
        """Running the Code tab's pipeline would mean writing the global
        coder_mode setting — flipping the switch under the user at 4am, and
        leaving it flipped if the process died mid-run."""
        source = (__import__("pathlib").Path(scheduled.__file__)).read_text(encoding="utf-8")
        assert "set_config" not in source
        assert "read_only_runner" in source

    def test_there_is_no_field_that_would_let_one_write(self):
        """A gate bypass is not something to add as a checkbox on the form for
        scheduling a morning summary."""
        source = (__import__("pathlib").Path(scheduled.__file__)).read_text(encoding="utf-8")
        assert "may_act" not in source


class TestTheApi:
    def test_a_task_can_be_made_listed_paused_and_deleted(self, client, isolated_db):
        made = client.post("/api/coder/scheduled", json={
            "prompt": "check the build", "schedule": "daily", "at": "07:30"}).json()
        assert made["at"] == "07:30"

        listed = client.get("/api/coder/scheduled").json()["tasks"]
        assert [t["id"] for t in listed] == [made["id"]]

        paused = client.patch(f"/api/coder/scheduled/{made['id']}",
                              json={"enabled": False}).json()
        assert paused["enabled"] is False

        assert client.delete(f"/api/coder/scheduled/{made['id']}").status_code == 200
        assert client.get("/api/coder/scheduled").json()["tasks"] == []

    def test_a_task_with_nothing_to_do_is_refused(self, client, isolated_db):
        assert client.post("/api/coder/scheduled", json={"prompt": "  "}).status_code == 400

    def test_editing_something_that_is_not_there_is_a_404(self, client, isolated_db):
        assert client.patch("/api/coder/scheduled/nope", json={"enabled": True}).status_code == 404
        assert client.delete("/api/coder/scheduled/nope").status_code == 404
        assert client.post("/api/coder/scheduled/nope/run").status_code == 404
