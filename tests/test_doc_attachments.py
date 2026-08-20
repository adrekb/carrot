"""A document reaches a conversation as an attachment, not as the message.

Sending a note to chat pasted the whole thing in as the turn. The transcript
opened with a wall of the user's own markdown — group markers and all — and the
question they actually wanted to ask had nowhere to go, because the message had
already been sent. A document is material; the question is the turn. So it
arrives the way every other file does: a chip above an empty box.

Two things had to be true for that to be an improvement rather than a move.

The markers had to stop travelling. They are HTML comments precisely so that
they are invisible when rendered and survive the editor untouched — but
"invisible" is not "absent", and the file is what gets sent, so a note with two
groups in it reached the model as prose interleaved with instruction-shaped
strings addressed to something that is not the model.

And the tray had to stop growing. It drew one full-width chip per file,
stacked, in the same strip of space as the resume bar and the document picker —
fine for one file and a layout bug for four.
"""
import re
from pathlib import Path

import pytest

from carrot import doc_agent

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"


def read(path):
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js():
    return read(WEB / "js" / "app.js")


@pytest.fixture(scope="module")
def docagent_js():
    return read(WEB / "js" / "docagent.js")


@pytest.fixture(scope="module")
def groups_js():
    return read(WEB / "js" / "docgroups.js")


class TestEditorSyntaxDoesNotReachTheModel:
    def test_group_markers_are_stripped_from_the_prompt(self, isolated_db):
        text = (
            "Rewrite plan.\n\n"
            "<!--carrot:group to=research/quick files=existing.py-->\n\n"
            "What is the state of the art?\n\n"
            "<!--/carrot:group-->\n\n"
            "Trailing."
        )
        prompt = doc_agent.resolve(text).prompt
        assert "carrot:group" not in prompt
        assert "Rewrite plan." in prompt and "Trailing." in prompt

    def test_routing_lines_go_too(self, isolated_db):
        """`@/to` says where the document is going, which is a fact about the
        send rather than part of the question — and a group's send writes them
        explicitly, so leaving them in staples `@/to/research/deep` to every
        routed paragraph."""
        prompt = doc_agent.resolve("the question\n\n@/to/research/deep\n@/model/local/phi4:14b").prompt
        assert prompt == "the question"

    def test_stripping_does_not_break_the_routing_it_strips(self, isolated_db):
        """Parsed before it is removed, or a clean prompt would cost the route."""
        resolved = doc_agent.resolve("q\n\n@/to/research/deep\n@/model/local/phi4:14b")
        assert (resolved.destination, resolved.option) == ("research", "deep")
        assert resolved.route.model == "phi4:14b"

    def test_a_citation_written_in_a_sentence_survives(self, isolated_db):
        """Only whole lines. `@/file/router.py` in the middle of a sentence is
        something the reader put there, and a paragraph that mentions its own
        sources should still read like one."""
        prompt = doc_agent.resolve("compare @/file/router.py with the plan").prompt
        assert "@/file/router.py" in prompt

    def test_no_blank_holes_are_left_behind(self, isolated_db):
        """A marker sat between blank lines, so removing it leaves three
        newlines where the document had one paragraph break."""
        prompt = doc_agent.resolve(
            "One.\n\n<!--carrot:group to=chat-->\n\nTwo.\n\n<!--/carrot:group-->\n\nThree."
        ).prompt
        assert "\n\n\n" not in prompt

    def test_the_staged_copy_is_stripped_too(self, groups_js, app_js):
        """A document staged into the composer never touches
        `/api/doc/send`, so the server's stripping does not cover it — the
        markers would ride to the model inside the .md itself, invisible in
        the chip."""
        assert "function stripGroupMarkers" in groups_js
        stage = app_js[app_js.index("function stageDocument"):]
        stage = stage[:stage.index("\n}")]
        assert "stripGroupMarkers" in stage


class TestChatStagesRatherThanSends:
    def test_chat_is_the_destination_that_waits(self, docagent_js):
        """Research and Agent fire immediately — "send this to Research" is
        already the whole instruction. Chat is a conversation, so the document
        is material and the question is still to be written."""
        dispatch = docagent_js[docagent_js.index("async function dispatchDoc"):]
        assert "stageDocument" in dispatch
        research_at = dispatch.index("streamResearchInto")
        stage_at = dispatch.index("stageDocument")
        assert research_at < stage_at, "research must still send immediately"

    def test_it_leaves_the_box_empty_and_focused(self, docagent_js):
        dispatch = docagent_js[docagent_js.index("async function dispatchDoc"):]
        assert "input.focus()" in dispatch

    def test_a_document_rides_the_attachment_pipeline(self, app_js):
        """Rather than a second one beside it: the server already turns a text
        attachment into prompt material for any model, and the tray already
        draws the chip."""
        stage = app_js[app_js.index("function stageDocument"):]
        stage = stage[:stage.index("\n}")]
        assert "pendingAttachments.push" in stage
        assert "text/markdown" in stage

    def test_bytes_not_characters(self, app_js):
        """`btoa` takes latin-1. A document with an em dash in it — which is to
        say most of them — throws without the encode step."""
        stage = app_js[app_js.index("function stageDocument"):]
        stage = stage[:stage.index("\n}")]
        assert "TextEncoder" in stage


class TestTheTrayIsOneLine:
    def test_it_counts_by_kind(self, app_js):
        assert "ATTACH_KINDS" in app_js
        kinds = re.search(r"const ATTACH_KINDS = \[(.*?)\n\];", app_js, re.DOTALL)
        assert kinds, "no kinds table"
        for kind in ("image", "pdf", "doc"):
            assert f"id: '{kind}'" in kinds.group(1)

    def test_one_attachment_still_shows_its_name(self, app_js):
        """Collapsing a single file to "1 document" is counting for its own
        sake, and it hides the name in the one case with room to show it."""
        render = app_js[app_js.index("function renderAttachTray"):]
        render = render[:render.index("\nfunction toggleAttachTray")]
        assert "pendingAttachments.length === 1" in render

    def test_the_counted_row_can_be_opened_to_remove_one(self, app_js):
        assert "function toggleAttachTray" in app_js
        assert "attachTrayOpen" in app_js

    def test_the_icons_it_names_exist(self):
        index_html = read(WEB / "index.html")
        for symbol in ("i-image", "i-file-pdf", "i-doc", "i-x"):
            assert f'id="{symbol}"' in index_html, f"no {symbol} in the sprite"


class TestOneSlashIsSkillsAndTwoIsDocuments:
    def test_the_skill_menu_does_not_claim_two(self, app_js):
        """Both opened on `//`: the picker offering documents, and the skill pop
        stacked over it filtering the catalogue for a skill named "/" and
        reporting, accurately and uselessly, that there were none."""
        changed = app_js[app_js.index("function cmdInputChanged"):]
        changed = changed[:changed.index("\n}")]
        assert "!val.startsWith('//')" in changed


class TestTheTrayAndTheContextPillAreNotTheSameObject:
    """They sat an inch apart, both pill-shaped, both counting, both saying
    "items" — five files in one and "2 items" in the other, both true and
    together illegible. They count different kinds of thing: a file rides one
    message, a source is standing and answers every turn until switched off."""

    def test_the_tray_says_what_it_is(self, app_js):
        render = app_js[app_js.index("function renderAttachTray"):]
        render = render[:render.index("\nfunction toggleAttachTray")]
        assert "attach-label" in render

    def test_context_counts_sources_not_items(self):
        context_js = read(WEB / "js" / "context.js")
        assert "' source'" in context_js
        assert "' item'" not in context_js, "the two counters should not share a noun"
