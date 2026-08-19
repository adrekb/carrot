"""A way to put a task into the scheduler.

`scheduled.py` has had the whole engine from the start — create, list, update,
delete, is_due — and `app.py` calls `start_scheduler()` at boot. It has been
running this entire time with nothing able to reach it: no routes, no UI. A
loop that wakes up, finds an empty table and goes back to sleep.

So these are the endpoints, and what they are careful about is the PATCH: a
scheduled task is edited one field at a time — paused, moved an hour — and a
PATCH that sent every field would turn "pause this" into "and also reset what
it does and when".
"""
import pytest

from carrot import scheduled


# `client` is conftest's: a TestClient carrying the session token, because
# every route here is behind auth like the rest of the API.


def make(client, prompt="check my assignments", **kw):
    body = {"prompt": prompt}
    body.update(kw)
    resp = client.post("/api/scheduled", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestPuttingSomethingOnTheSchedule:

    def test_a_task_can_be_created_and_comes_back_in_the_list(self, client):
        made = make(client, schedule="weekly", at="17:30", weekday="friday")
        assert made["prompt"] == "check my assignments"
        assert (made["schedule"], made["at"], made["weekday"]) == ("weekly", "17:30", "friday")
        assert made["enabled"]

        listed = client.get("/api/scheduled").json()
        assert [t["id"] for t in listed["tasks"]] == [made["id"]]

    def test_the_list_says_what_the_choices_are(self, client):
        """The form is built from this rather than from a copy of it in the
        markup, which is how a schedule the engine dropped stays offerable."""
        data = client.get("/api/scheduled").json()
        assert data["schedules"] == list(scheduled.SCHEDULES)
        assert "monday" in data["weekdays"]

    def test_an_empty_prompt_is_refused_in_the_engines_own_words(self, client):
        resp = client.post("/api/scheduled", json={"prompt": "   "})
        assert resp.status_code == 400
        assert "something to do" in resp.json()["detail"]

    def test_a_bad_schedule_falls_back_rather_than_failing(self, client):
        """The engine already decides this. The route does not get a second
        opinion about it."""
        made = make(client, schedule="fortnightly")
        assert made["schedule"] == scheduled.EVERY_DAY


class TestChangingOne:

    def test_pausing_changes_only_enabled(self, client):
        made = make(client, schedule="weekly", at="17:30", weekday="friday")
        paused = client.patch(f"/api/scheduled/{made['id']}",
                              json={"enabled": False}).json()
        assert not paused["enabled"]
        # Everything else survived the pause.
        assert (paused["prompt"], paused["schedule"], paused["at"], paused["weekday"]) == \
               (made["prompt"], "weekly", "17:30", "friday")

    def test_resuming_works_too(self, client):
        made = make(client)
        client.patch(f"/api/scheduled/{made['id']}", json={"enabled": False})
        back = client.patch(f"/api/scheduled/{made['id']}", json={"enabled": True}).json()
        assert back["enabled"]

    def test_editing_a_missing_task_is_a_404_not_a_silent_nothing(self, client):
        assert client.patch("/api/scheduled/nope", json={"enabled": False}).status_code == 404


class TestTakingOneOff:

    def test_delete_removes_it(self, client):
        made = make(client)
        assert client.delete(f"/api/scheduled/{made['id']}").json() == {"deleted": True}
        assert client.get("/api/scheduled").json()["tasks"] == []

    def test_deleting_a_missing_task_is_a_404(self, client):
        assert client.delete("/api/scheduled/nope").status_code == 404
