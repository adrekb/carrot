"""Copy, rerun and branch — the three things you do to a message you have read.

A transcript is not a log you only scroll. You want a paragraph out of it, you
want the answer again because the first one missed, and you want to ask the
question differently without losing the answer you are comparing against.

The three differ in what they do to history, and that is the whole design:

* **Copy** touches nothing.
* **Rerun** replaces — the old answer has to be gone, or asking again means
  nothing and the model is handed a transcript in which it has already
  answered.
* **Branch** copies — rewriting the original would destroy the answer you
  wanted to compare against, which is the reason you branched.
"""
from pathlib import Path

import pytest

from carrot import conversation as conv_mod, workspaces


def read_js(name="app.js"):
    root = Path(__file__).resolve().parents[1]
    return (root / "carrot" / "web" / "js" / name).read_text(encoding="utf-8")


@pytest.fixture
def thread(isolated_db):
    conv = conv_mod.create_conversation(title="Routing question")
    conv_mod.add_message(conv["id"], "user", "How does routing work?")
    conv_mod.add_message(conv["id"], "assistant", "It maps a task to a model.")
    conv_mod.add_message(conv["id"], "user", "And with no assignment?")
    conv_mod.add_message(conv["id"], "assistant", "It runs on-device.")
    return conv_mod.get_conversation(conv["id"])


class TestBranch:
    def test_it_keeps_everything_up_to_and_including_the_message(self, thread):
        second = thread["messages"][1]["id"]
        branch = conv_mod.branch_conversation(thread["id"], second)
        assert [m["content"] for m in branch["messages"]] == [
            "How does routing work?",
            "It maps a task to a model.",
        ]

    def test_the_original_is_untouched(self, thread):
        # The whole point: the answer you are comparing against has to survive.
        conv_mod.branch_conversation(thread["id"], thread["messages"][1]["id"])
        assert len(conv_mod.get_conversation(thread["id"])["messages"]) == 4

    def test_the_branch_is_a_real_copy(self, thread):
        branch = conv_mod.branch_conversation(thread["id"], thread["messages"][1]["id"])
        # Different rows, so deleting the parent cannot empty the branch.
        original_ids = {m["id"] for m in thread["messages"]}
        assert not original_ids & {m["id"] for m in branch["messages"]}
        conv_mod.delete_conversation(thread["id"])
        assert len(conv_mod.get_conversation(branch["id"])["messages"]) == 2

    def test_it_records_where_it_came_from(self, thread):
        branch = conv_mod.branch_conversation(thread["id"], thread["messages"][1]["id"])
        assert branch["metadata"]["branched_from"] == thread["id"]
        assert branch["metadata"]["branched_at_message"] == thread["messages"][1]["id"]

    def test_branching_the_last_message_copies_everything(self, thread):
        branch = conv_mod.branch_conversation(thread["id"], thread["messages"][-1]["id"])
        assert len(branch["messages"]) == 4

    def test_a_title_can_be_given(self, thread):
        branch = conv_mod.branch_conversation(thread["id"], thread["messages"][0]["id"],
                                              title="Other approach")
        assert branch["title"] == "Other approach"

    def test_an_unknown_message_is_refused(self, thread):
        with pytest.raises(ValueError):
            conv_mod.branch_conversation(thread["id"], 999999)

    def test_an_unknown_conversation_is_refused(self, isolated_db):
        with pytest.raises(ValueError):
            conv_mod.branch_conversation("nope", 1)

    def test_it_lands_in_the_parent_s_workspace(self, thread):
        # A fork of a project's conversation belongs with the project, not in
        # whatever workspace happens to be active when you click Branch.
        space = workspaces.create_workspace("Thesis")["id"]
        workspaces.file_item(workspaces.KIND_CONVERSATION, thread["id"], space)
        other = workspaces.create_workspace("Elsewhere")["id"]
        workspaces.set_active_workspace(other)

        branch = conv_mod.branch_conversation(thread["id"], thread["messages"][1]["id"])
        assert workspaces.workspace_of(
            workspaces.KIND_CONVERSATION, branch["id"]) == space


class TestRewind:
    def test_it_drops_the_message_and_everything_after(self, thread):
        removed = conv_mod.drop_messages_from(thread["id"], thread["messages"][3]["id"])
        assert removed == 1
        assert [m["content"] for m in conv_mod.get_conversation(thread["id"])["messages"]] == [
            "How does routing work?",
            "It maps a task to a model.",
            "And with no assignment?",
        ]

    def test_rewinding_from_the_middle_takes_the_tail_with_it(self, thread):
        # A transcript with a hole in the middle is worse than either keeping
        # or dropping the lot.
        conv_mod.drop_messages_from(thread["id"], thread["messages"][1]["id"])
        assert len(conv_mod.get_conversation(thread["id"])["messages"]) == 1

    def test_rewinding_nothing_is_not_an_error(self, thread):
        assert conv_mod.drop_messages_from(thread["id"], 999999) == 0


class TestTheApi:
    def make(self, client):
        conv = client.post("/api/conversations", json={"title": "T"}).json()
        client.post(f"/api/conversations/{conv['id']}/messages",
                    json={"role": "user", "content": "Question?"})
        client.post(f"/api/conversations/{conv['id']}/messages",
                    json={"role": "assistant", "content": "Answer."})
        return client.get(f"/api/conversations/{conv['id']}").json()

    def test_branching_over_http(self, client):
        conv = self.make(client)
        branch = client.post(f"/api/conversations/{conv['id']}/branch",
                             json={"message_id": conv["messages"][0]["id"]}).json()
        assert [m["content"] for m in branch["messages"]] == ["Question?"]

    def test_branching_an_unknown_message_is_a_404(self, client):
        conv = self.make(client)
        assert client.post(f"/api/conversations/{conv['id']}/branch",
                           json={"message_id": 999999}).status_code == 404

    def test_rewinding_over_http(self, client):
        conv = self.make(client)
        body = client.post(f"/api/conversations/{conv['id']}/rewind",
                           json={"message_id": conv["messages"][1]["id"]}).json()
        assert body["removed"] == 1

    def test_rewinding_an_unknown_conversation_is_a_404(self, client):
        assert client.post("/api/conversations/nope/rewind",
                           json={"message_id": 1}).status_code == 404


class TestReplayDoesNotDuplicateTheQuestion:
    """Rerun re-asks a question that is already in the transcript."""

    def test_a_replayed_turn_does_not_store_the_question_again(self, client):
        conv = client.post("/api/conversations", json={"title": "T"}).json()
        client.post(f"/api/conversations/{conv['id']}/messages",
                    json={"role": "user", "content": "Say hello"})
        client.post("/api/chat", json={
            "message": "Say hello", "conversation_id": conv["id"], "replay": True,
            "search_mode": "off",
        })
        roles = [m["role"] for m in client.get(f"/api/conversations/{conv['id']}").json()["messages"]]
        assert roles == ["user", "assistant"]

    def test_an_ordinary_turn_still_stores_it(self, client):
        conv = client.post("/api/conversations", json={"title": "T"}).json()
        client.post("/api/chat", json={
            "message": "Say hello", "conversation_id": conv["id"], "search_mode": "off",
        })
        roles = [m["role"] for m in client.get(f"/api/conversations/{conv['id']}").json()["messages"]]
        assert roles == ["user", "assistant"]

    def test_the_prompt_does_not_carry_the_question_twice(self, isolated_db, fake_ollama):
        # Appending it again would hand the model the same question twice in a
        # row, which reads as insistence rather than as a retry.
        from carrot import app as A

        conv = conv_mod.create_conversation(title="T")
        conv_mod.add_message(conv["id"], "user", "Say hello")
        history, _ = A._prepare_history(
            conv_mod.get_conversation(conv["id"]), "Say hello", None, replay=True)
        assert [m["content"] for m in history if m["role"] == "user"] == ["Say hello"]


class TestTheButtons:
    def test_copy_hands_back_the_markdown_not_the_html(self):
        # innerText flattens the formatting out of it, which is not what was
        # said and not what anyone wants to paste.
        source = read_js()
        assert "div.dataset.raw = content" in source
        assert "div.dataset.raw || div.querySelector('.content')?.innerText" in source

    def test_rerun_only_appears_on_the_last_answer(self):
        # Replacing a message from the middle would silently discard everything
        # after it. That is what Branch is for.
        source = read_js()
        assert "isLastMessage(div)" in source

    def test_rerun_clears_the_old_answer_before_asking(self):
        source = read_js()
        assert source.index("/rewind") < source.index("await streamTurn('/api/chat/stream', {\n        message: question")

    def test_actions_needing_an_id_are_not_offered_without_one(self):
        assert "if (div.dataset.messageId) {" in read_js()

    def test_ids_arrive_without_reopening_the_conversation(self):
        # The moment you most want "run that again" is right after reading the
        # answer, not after a reload.
        assert "await syncMessageIds();" in read_js()

    def test_id_matching_stops_on_a_role_mismatch(self):
        # Hanging an id on the wrong message is how Rerun deletes something
        # nobody pointed at.
        assert "if (!div.classList.contains(message.role)) break;" in read_js()

    def test_the_actions_are_reachable_by_keyboard(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "carrot" / "web" / "css" / "style.css").read_text(encoding="utf-8")
        assert ".msg-actions:focus-within" in css
        assert ".msg-action:focus-visible" in css

    def test_they_do_not_shift_the_transcript_when_revealed(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "carrot" / "web" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index(".msg-actions {"):css.index(".message:hover .msg-actions")]
        assert "opacity: 0;" in block and "display: none" not in block

    def test_the_icons_they_name_exist(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "carrot" / "web" / "index.html").read_text(encoding="utf-8")
        for icon in ("i-clipboard", "i-refresh", "i-branch"):
            assert f'id="{icon}"' in html
