"""Canvas and slides are document formats, not places.

They live in the notes directory, are listed in the same sidebar and are
opened by the same click. What differs is the editor, which `format` already
decided for LaTeX. These tests pin the parts of that which are easy to break
from a long way away.
"""
import json

from carrot import notes


class TestTheyAreRealFormats:
    def test_a_canvas_records_what_it_is(self, isolated_db):
        made = notes.create_note("Board", "{}", doc_format=notes.FORMAT_CANVAS)
        assert notes.get_note(made["id"])["format"] == notes.FORMAT_CANVAS

    def test_a_deck_records_what_it_is(self, isolated_db):
        made = notes.create_note("Deck", "# One", doc_format=notes.FORMAT_SLIDES)
        assert notes.get_note(made["id"])["format"] == notes.FORMAT_SLIDES

    def test_editing_does_not_turn_them_into_markdown(self, isolated_db):
        """The frontmatter is rewritten on every save, and a canvas that
        becomes markdown is a canvas that opens in a prose editor showing its
        own JSON."""
        made = notes.create_note("Board", "{}", doc_format=notes.FORMAT_CANVAS)
        notes.update_note(made["id"], '{"nodes": [], "edges": []}')
        assert notes.get_note(made["id"])["format"] == notes.FORMAT_CANVAS


class TestASeparatorIsNotFrontmatter:
    """A deck is markdown with `---` between slides, stored in a file whose
    frontmatter is also delimited by `---`. The split is bounded to the first
    two, and this is the test that says so — a deck losing every slide after
    the first is the failure, and it would only show up in the editor."""

    def test_a_deck_keeps_its_slide_separators(self, isolated_db):
        deck = "# One\n\n---\n\n# Two\n\n---\n\n# Three\n"
        made = notes.create_note("Deck", deck, doc_format=notes.FORMAT_SLIDES)
        assert notes.get_note(made["id"])["body"] == deck

    def test_they_survive_a_save(self, isolated_db):
        deck = "# One\n\n---\n\n# Two\n"
        made = notes.create_note("Deck", "", doc_format=notes.FORMAT_SLIDES)
        notes.update_note(made["id"], deck)
        assert notes.get_note(made["id"])["body"] == deck

    def test_a_deck_that_opens_with_a_separator_still_parses(self, isolated_db):
        """The body starting with `---` is the case where a naive split reads
        the first slide as a second frontmatter block."""
        deck = "---\n\n# Only\n"
        made = notes.create_note("Deck", deck, doc_format=notes.FORMAT_SLIDES)
        assert "# Only" in notes.get_note(made["id"])["body"]


class TestACanvasRoundTrips:
    def test_the_json_comes_back_exactly(self, isolated_db):
        board = {"nodes": [{"id": "b1", "x": 10, "y": 20, "w": 220, "h": 120,
                            "title": "Lecture 1", "text": "notes"}],
                 "edges": [{"from": "b1", "to": "b1"}]}
        made = notes.create_note("Board", json.dumps(board), doc_format=notes.FORMAT_CANVAS)
        assert json.loads(notes.get_note(made["id"])["body"]) == board

    def test_a_colon_in_a_box_name_does_not_corrupt_the_file(self, isolated_db):
        """The frontmatter parser splits on the first colon of every line, so a
        body line containing one is the obvious way to break it."""
        board = {"nodes": [{"id": "b1", "title": "Lecture 1: Rationalism"}], "edges": []}
        made = notes.create_note("Board", json.dumps(board), doc_format=notes.FORMAT_CANVAS)
        got = json.loads(notes.get_note(made["id"])["body"])
        assert got["nodes"][0]["title"] == "Lecture 1: Rationalism"
