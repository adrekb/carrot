"""Every document format, as text a model can read.

Two of the four formats are prose and need no rendering. The other two are not:
a deck is a JSON array of positioned boxes and a canvas is an Excalidraw scene,
so "send this to chat" on either meant sending several kilobytes of
coordinates, element ids and style keys — which is why neither had a Send
button at all, and why the Write tab's Send has only ever existed in prose
mode.

Rendered server-side rather than in each editor: one renderer these tests can
hold, and every client path gets the same reading rather than three that drift.
"""
import json

import pytest

from carrot import doctext


def deck(*slides):
    return json.dumps({"slides": list(slides)})


def slide(*elements, notes=""):
    out = {"elements": list(elements)}
    if notes:
        out["notes"] = notes
    return out


class TestProsePassesThrough:
    @pytest.mark.parametrize("fmt", ["markdown", "latex"])
    def test_it_is_returned_unchanged(self, fmt):
        """These are already the thing. Wrapping them in a heading here would
        double up the one the caller adds."""
        body = "# Title\n\nA paragraph."
        assert doctext.as_text(body, fmt) == body

    def test_an_unknown_format_is_treated_as_prose(self):
        """A format added to the app and not to this file must not blank the
        document — the wrong rendering is recoverable, no rendering is not."""
        assert doctext.as_text("hello", "something-new") == "hello"

    def test_an_empty_body_is_empty(self):
        assert doctext.as_text("", "markdown") == ""
        assert doctext.as_text(None, "markdown") == ""


class TestADeckReadsAsAnOutline:
    def test_the_slides_are_numbered(self):
        text = doctext.as_text(deck(slide(), slide()), "slides")
        assert "## Slide 1" in text
        assert "## Slide 2" in text
        assert "2 slides" in text

    def test_one_slide_is_singular(self):
        assert "1 slide\n" in doctext.as_text(deck(slide()), "slides") + "\n"

    def test_content_comes_before_geometry(self):
        """A model asked to rewrite the second bullet should not have to parse
        a layout to find it."""
        text = doctext.as_text(
            deck(slide({"type": "text", "text": "The point", "x": 10, "y": 20, "w": 30, "h": 40})),
            "slides")
        line = next(l for l in text.splitlines() if "The point" in l)
        assert line.index("The point") < line.index("at 10,20")

    def test_every_element_is_addressable(self):
        """The point is not only reading. "Element 3 on slide 2" is an edit
        that can be applied without re-serialising the whole document."""
        text = doctext.as_text(
            deck(slide({"type": "text", "text": "a"}, {"type": "rect"}, {"type": "line"})),
            "slides")
        assert "[0]" in text and "[1]" in text and "[2]" in text

    def test_a_shape_is_named_by_its_shape(self):
        text = doctext.as_text(deck(slide({"type": "cylinder", "text": "Postgres"})), "slides")
        assert "shape cylinder" in text
        assert '"Postgres"' in text

    def test_an_empty_slide_says_so(self):
        """A slide with nothing on it renders as one, not as an absence — a
        model that cannot see the blank slide will not notice it is there."""
        assert "(empty)" in doctext.as_text(deck(slide()), "slides")

    def test_speaker_notes_come_through(self):
        text = doctext.as_text(deck(slide(notes="mention the budget")), "slides")
        assert "Notes: mention the budget" in text

    def test_an_embedded_image_is_not_pasted_in(self):
        """A data: URI is a whole image. Its length is not information, and it
        would spend the entire context on one slide."""
        big = "data:image/png;base64," + ("A" * 40000)
        text = doctext.as_text(deck(slide({"type": "image", "src": big})), "slides")
        assert "embedded image" in text
        assert "AAAA" not in text
        assert len(text) < 500

    def test_a_long_text_box_is_clipped_and_says_so(self):
        text = doctext.as_text(deck(slide({"type": "text", "text": "x" * 5000})), "slides")
        assert "chars)" in text
        assert len(text) < 1200

    def test_a_deck_still_in_markdown_is_returned_as_itself(self):
        """The editor converts markdown decks on open, so an unconverted one is
        still markdown on disk. That *is* the document, and it happens to
        already be prose."""
        source = "# Slide one\n---\n# Slide two"
        assert doctext.as_text(source, "slides") == source

    def test_geometry_is_omitted_when_it_is_not_numbers(self):
        """Half a position is worse than none: "at 10,undefined" reads as a
        fact about the slide."""
        text = doctext.as_text(deck(slide({"type": "rect", "x": 10, "y": None})), "slides")
        assert "at " not in text


class TestACanvasReadsAsAList:
    def test_the_elements_are_listed(self):
        body = json.dumps({"elements": [
            {"type": "rectangle", "x": 1, "y": 2, "width": 3, "height": 4},
            {"type": "text", "text": "Heat exchanger", "x": 5, "y": 6, "width": 7, "height": 8},
        ]})
        text = doctext.as_text(body, "canvas")
        assert "2 elements" in text
        assert "rectangle" in text
        assert '"Heat exchanger"' in text

    def test_deleted_elements_are_skipped(self):
        """Excalidraw tombstones rather than removes, so a scene worked in for
        an hour carries every shape ever drawn. Describing those is describing
        a canvas the user cannot see."""
        body = json.dumps({"elements": [
            {"type": "rectangle"},
            {"type": "ellipse", "isDeleted": True},
        ]})
        text = doctext.as_text(body, "canvas")
        assert "1 element" in text
        assert "ellipse" not in text

    def test_an_empty_canvas_says_so(self):
        assert "(empty)" in doctext.as_text(json.dumps({"elements": []}), "canvas")

    def test_a_bare_array_is_accepted(self):
        """Some scenes are stored as the element array rather than the scene
        object, and refusing one of the two shapes is refusing half the files."""
        text = doctext.as_text(json.dumps([{"type": "rectangle"}]), "canvas")
        assert "1 element" in text

    def test_unparseable_is_returned_as_itself(self):
        assert doctext.as_text("not json", "canvas") == "not json"


class TestTheEndpoint:
    def test_it_renders_the_note(self, client):
        from carrot import notes as notes_mod

        note = notes_mod.create_note(
            title="Pipeline",
            content=deck(slide({"type": "text", "text": "Ingest"})),
            doc_format="slides")
        payload = client.get(f"/api/notes/{note['id']}/text").json()
        assert payload["format"] == "slides"
        assert "## Slide 1" in payload["text"]
        assert "Ingest" in payload["text"]

    def test_the_raw_note_is_still_the_json(self, client):
        """The rendering is for reading. A deck's source of truth is its JSON,
        and a round trip through prose would drop the fills, rotations and
        z-order the visual editor exists to set."""
        from carrot import notes as notes_mod

        note = notes_mod.create_note(
            title="Pipeline",
            content=deck(slide({"type": "text", "text": "Ingest"})),
            doc_format="slides")
        raw = client.get(f"/api/notes/{note['id']}").json()
        assert raw["body"].lstrip().startswith("{")

    def test_a_missing_note_is_a_404(self, client):
        assert client.get("/api/notes/nope/text").status_code == 404


class TestEveryEditorCanReachChat:
    """Prose has had a Send since it existed; the other three never got one,
    because what they hold is not text. Now that it can be rendered, they do."""

    @pytest.mark.parametrize("pane", ["slides-pane", "canvas-pane", "latex-pane"])
    def test_the_pane_has_a_send(self, pane):
        from pathlib import Path

        index = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "index.html"
                 ).read_text(encoding="utf-8")
        start = index.index(f'id="{pane}"')
        end = index.index("</section>", start)
        assert "sendDocumentToChat()" in index[start:end], pane

    def test_it_uses_the_server_rendering(self):
        """Not a fourth copy of the renderer in the browser."""
        from pathlib import Path

        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js" / "docagent.js"
              ).read_text(encoding="utf-8")
        assert "/text`" in js

    def test_it_lands_in_the_composer_rather_than_sending(self):
        """A deck arriving in chat with no question attached is a wall of
        outline and no turn. What you want to say about it is the point."""
        from pathlib import Path

        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js" / "docagent.js"
              ).read_text(encoding="utf-8")
        import re
        body = re.search(r"async function sendDocumentToChat\(\)\s*\{(.*?)\n\}", js, re.DOTALL)
        assert body, "sendDocumentToChat not found"
        assert "cmd-input" in body.group(1)
        assert "sendChat()" not in body.group(1)
