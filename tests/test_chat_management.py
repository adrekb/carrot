"""Tests for chat management (folders, star, delete) and new QoL widgets."""
import pytest


# ===== Chat folders =====

def test_folder_crud(client):
    # Create.
    resp = client.post("/api/chat-folders", json={"name": "Work"})
    assert resp.status_code == 200
    folder = resp.json()
    assert folder["name"] == "Work"
    fid = folder["id"]

    # List.
    folders = client.get("/api/chat-folders").json()
    assert any(f["id"] == fid for f in folders)

    # Rename.
    resp = client.put(f"/api/chat-folders/{fid}", json={"name": "Job"})
    assert resp.status_code == 200
    folders = client.get("/api/chat-folders").json()
    assert next(f for f in folders if f["id"] == fid)["name"] == "Job"

    # Delete.
    assert client.delete(f"/api/chat-folders/{fid}").status_code == 200
    assert all(f["id"] != fid for f in client.get("/api/chat-folders").json())


def test_star_and_folder_assignment(client):
    conv = client.post("/api/conversations", json={"title": "Hi"}).json()
    cid = conv["id"]
    folder = client.post("/api/chat-folders", json={"name": "Study"}).json()
    fid = folder["id"]

    # Star it.
    resp = client.patch(f"/api/conversations/{cid}", json={"starred": True})
    assert resp.status_code == 200
    assert resp.json()["metadata"]["starred"] is True

    # Move into folder.
    resp = client.patch(f"/api/conversations/{cid}", json={"folder_id": fid})
    assert resp.json()["metadata"]["folder_id"] == fid
    # Star preserved across a separate meta update.
    assert resp.json()["metadata"]["starred"] is True

    # Clear folder (empty string unassigns).
    resp = client.patch(f"/api/conversations/{cid}", json={"folder_id": ""})
    assert "folder_id" not in resp.json()["metadata"]


def test_delete_conversation(client):
    conv = client.post("/api/conversations", json={"title": "Bye"}).json()
    cid = conv["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"role": "user", "content": "hello"})

    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    # Deleting again -> 404.
    assert client.delete(f"/api/conversations/{cid}").status_code == 404


def test_delete_folder_unassigns_chats(client):
    conv = client.post("/api/conversations", json={"title": "In folder"}).json()
    cid = conv["id"]
    folder = client.post("/api/chat-folders", json={"name": "Temp"}).json()
    fid = folder["id"]
    client.patch(f"/api/conversations/{cid}", json={"folder_id": fid})

    client.delete(f"/api/chat-folders/{fid}")
    meta = client.get(f"/api/conversations/{cid}").json()["metadata"]
    assert "folder_id" not in meta


def test_patch_missing_conversation_404(client):
    assert client.patch("/api/conversations/nope", json={"starred": True}).status_code == 404


# ===== New quality-of-life widgets =====

def test_catalog_includes_qol_widgets(client):
    catalog = client.get("/api/widgets/catalog").json()["catalog"]
    types = {item["type"] for item in catalog}
    assert {"calendar", "weather", "quicknotes"} <= types


def test_add_calendar_widget_defaults_to_canvas(client):
    w = client.post("/api/widgets", json={"type": "calendar"}).json()
    assert w["type"] == "calendar"
    assert w["slot"] == "canvas"


def test_add_weather_and_quicknotes(client):
    wx = client.post("/api/widgets", json={"type": "weather"}).json()
    assert wx["config"]["units"] == "c"
    assert wx["config"]["lat"] is None

    qn = client.post("/api/widgets", json={"type": "quicknotes"}).json()
    assert qn["config"]["note_id"] == ""


def test_weather_location_saved_to_config(client):
    w = client.post("/api/widgets", json={"type": "weather"}).json()
    resp = client.put(
        f"/api/widgets/{w['id']}",
        json={"config": {"lat": 51.5, "lon": -0.12, "label": "London"}},
    )
    assert resp.status_code == 200
    cfg = resp.json()["config"]
    assert cfg["lat"] == 51.5
    assert cfg["label"] == "London"
    # Default keys preserved on merge.
    assert cfg["units"] == "c"


class TestTheHistoryIsABudgetNotAMessageCount:
    """A count of messages is not a size.

    Twenty short exchanges are 18k characters and twenty long ones are 152k —
    measured, not guessed, on a real database whose largest stored answer is
    7,599 characters. At the top of that range the recent window alone is 116%
    of a 32k context, and a turn that reads three pages adds another 44%.

    Nothing errors when a request exceeds the window. The provider drops the
    *front* of the prompt, which is where the system directive and the tool
    definitions live, so the model answers without either — the same failure as
    running in a 4k window, arriving from the other end.
    """

    def messages(self, count, each):
        return [{"role": "assistant" if i % 2 else "user", "content": "x" * each}
                for i in range(count)]

    def test_a_short_conversation_is_kept_whole(self):
        from carrot import summarize
        kept = summarize._within_budget(self.messages(20, 900))
        assert len(kept) == 20

    def test_a_long_one_is_trimmed_to_fit(self):
        from carrot import summarize
        kept = summarize._within_budget(self.messages(20, 7599))
        assert len(kept) < 20
        assert sum(len(m["content"]) for m in kept) <= summarize.HISTORY_BUDGET_CHARS * 1.2

    def test_it_keeps_the_newest_not_the_oldest(self):
        """Recency is what the budget buys. Anything dropped is already in the
        rolling summary above it."""
        from carrot import summarize
        msgs = [{"role": "user", "content": f"{i}" + "x" * 20000} for i in range(10)]
        kept = summarize._within_budget(msgs)
        assert kept[-1]["content"].startswith("9")

    def test_order_is_preserved(self):
        from carrot import summarize
        msgs = [{"role": "user", "content": str(i)} for i in range(6)]
        assert [m["content"] for m in summarize._within_budget(msgs)] == list("012345")

    def test_one_enormous_answer_cannot_evict_its_own_question(self):
        from carrot import summarize
        kept = summarize._within_budget(
            [{"role": "user", "content": "q"},
             {"role": "assistant", "content": "y" * 200000}])
        assert kept[0]["content"] == "q"

    def test_there_is_a_floor(self):
        from carrot import summarize
        kept = summarize._within_budget(self.messages(10, 99999))
        assert len(kept) >= summarize.MIN_RECENT_MESSAGES

    def test_the_budget_leaves_room_for_the_rest_of_the_turn(self):
        """The tools, the directive and the pages a turn reads all have to fit
        beside it inside the same window."""
        from carrot import summarize
        window_chars = 32768 * 4
        assert summarize.HISTORY_BUDGET_CHARS <= window_chars * 0.4
