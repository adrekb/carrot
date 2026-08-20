"""Pointing at a document you have already written, from the composer.

What this replaces is describing your own notes to the model: the Q3 plan is
open in the next tab, and the only way to get it into the answer was to select
it, copy it, and paste a wall of it into the chat box — losing its title, and
leaving the conversation carrying a copy that goes stale the moment the
original is edited.
"""
from pathlib import Path

import pytest

from carrot import app as app_mod
from carrot import notes as notes_mod, workspaces as workspaces_mod

WEB = Path(__file__).resolve().parents[1] / "carrot" / "web"


@pytest.fixture
def plan(isolated_db):
    return notes_mod.create_note("Q3 plan", "Ship the tailnet pairing. Then doc tabs.")


class TestALinkIsRead:
    def test_the_document_is_fetched_at_send_time(self, plan):
        """A reference, not a copy: edit the note afterwards and the next turn
        sees the new version."""
        prompt = app_mod.linked_documents_prompt("have a look at [[Q3 plan]]")
        assert "Ship the tailnet pairing" in prompt

    def test_a_link_to_nothing_is_silent(self, isolated_db):
        """`[[something]]` that matches no document is a typo or a link to a
        page not written yet. Both are ordinary; neither is worth failing a
        turn that is otherwise fine."""
        assert app_mod.linked_documents_prompt("see [[no such document]]") == ""

    def test_a_plain_message_carries_nothing(self, isolated_db):
        assert app_mod.linked_documents_prompt("just a question") == ""

    def test_it_joins_what_was_attached_rather_than_replacing_it(self, plan):
        combined = app_mod.with_linked_documents("read [[Q3 plan]]", "AN ATTACHED FILE")
        assert "AN ATTACHED FILE" in combined
        assert "Ship the tailnet pairing" in combined

    def test_only_a_few_documents_per_turn(self, isolated_db):
        """Two documents is a reference; ten is a way to fill the window by
        accident."""
        for n in range(8):
            notes_mod.create_note(f"Doc {n}", f"body {n}")
        message = " ".join(f"[[Doc {n}]]" for n in range(8))
        prompt = app_mod.linked_documents_prompt(message)
        assert prompt.count("--- Doc") <= app_mod.MAX_LINKED_DOCS

    def test_the_same_document_twice_is_read_once(self, plan):
        prompt = app_mod.linked_documents_prompt("[[Q3 plan]] and again [[q3 plan]]")
        assert prompt.count("Ship the tailnet pairing") == 1


class TestEveryPathThatBuildsAPromptKnows:
    """The Context panel is built by the same function as the real prompt for a
    reason. A linked document that the model reads and the panel does not
    mention would make the one screen whose job is "what is Carrot being told"
    the only place that did not know."""

    @pytest.mark.parametrize("count", [3])
    def test_the_two_chat_paths_and_the_preview(self, count):
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        assert source.count("with_linked_documents(") >= count

    def test_the_preview_reports_it_as_a_document(self, client, plan):
        payload = client.get("/api/context",
                             params={"message": "summarise [[Q3 plan]]"}).json()
        document = next(s for s in payload["sources"] if s["id"] == "document")
        assert document["present"] is True
        assert "Ship the tailnet pairing" in document["preview"]

    def test_without_a_link_that_source_is_empty(self, client, isolated_db):
        payload = client.get("/api/context", params={"message": "summarise it"}).json()
        document = next(s for s in payload["sources"] if s["id"] == "document")
        assert document["present"] is False


class TestTheWorkspaceIsAFilter:
    """You cannot link a workspace: it is not a document, and attaching one
    would mean attaching everything in it. What it is good for is narrowing two
    hundred documents to the four in the project you are in."""

    def test_candidates_offer_documents_and_the_workspaces_to_filter_by(self, client, plan):
        body = client.get("/api/link/candidates").json()
        assert any(d["title"] == "Q3 plan" for d in body["documents"])
        assert "workspaces" in body

    def test_filtering_narrows_to_what_is_filed_there(self, client, plan, isolated_db):
        notes_mod.create_note("Unfiled note", "not in the workspace")
        space = workspaces_mod.create_workspace(name="Thesis")
        workspaces_mod.file_item(workspaces_mod.KIND_NOTE, plan["id"], space["id"])

        everything = client.get("/api/link/candidates").json()["documents"]
        inside = client.get("/api/link/candidates",
                            params={"workspace": space["id"]}).json()["documents"]
        assert len(inside) < len(everything)
        assert [d["title"] for d in inside] == ["Q3 plan"]

    def test_a_workspace_is_never_offered_as_something_to_link(self, client, plan):
        body = client.get("/api/link/candidates").json()
        assert all("format" in d for d in body["documents"])


class TestThePicker:
    def test_it_is_loaded_and_opens_on_the_apps_own_link_syntax(self):
        """`[[` is what this picker inserts, so typing the opening bracket and
        being offered what goes inside it is as direct as a trigger gets — and
        it leaves `/` to mean commands, which is what it means in every app
        people arrive here from."""
        index = (WEB / "index.html").read_text(encoding="utf-8")
        assert '<script src="/js/linkpicker.js"></script>' in index
        js = (WEB / "js" / "linkpicker.js").read_text(encoding="utf-8")
        trigger = js.split("const LINK_TRIGGER = ")[1].splitlines()[0]
        assert "\\[\\[" in trigger, trigger

    def test_both_triggers_are_advertised_where_they_are_typed(self):
        index = (WEB / "index.html").read_text(encoding="utf-8")
        assert "/ for skills" in index
        assert "[[ to link a document" in index

    def test_a_url_does_not_open_it(self):
        """`//` still works, but only at the start of a message or after a
        space. Without that guard, typing `https://` opened a document picker
        over the URL somebody was in the middle of pasting."""
        js = (WEB / "js" / "linkpicker.js").read_text(encoding="utf-8")
        trigger = js.split("const LINK_TRIGGER = ")[1].splitlines()[0]
        assert "(?:^|" in trigger, trigger

    def test_it_writes_the_same_link_syntax_notes_use(self):
        """So a reference means the same thing wherever it is written."""
        js = (WEB / "js" / "linkpicker.js").read_text(encoding="utf-8")
        assert "`[[${doc.title}]] `" in js

    def test_enter_only_belongs_to_the_menu_while_it_is_open(self):
        """Every other time it has to send the message."""
        js = (WEB / "js" / "linkpicker.js").read_text(encoding="utf-8")
        guard = js[js.index("function linkMenuKeydown"):][:300]
        assert "if (!linkMenuOpen()" in guard
        app_js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
        assert "linkMenuKeydown(event)" in app_js
