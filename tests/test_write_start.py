"""Write opens with what you can start and what you were writing.

"Select a note on the left, or create a new one" is a sentence telling somebody
about a thing they can already see. Docs opens with the documents you can
begin and the ones you were last in, which is a whole screen doing work rather
than apologising for being empty.
"""
from pathlib import Path

from carrot import notes


def read(*parts):
    return (Path(__file__).resolve().parents[1] / "carrot" / "web"
            ).joinpath(*parts).read_text(encoding="utf-8")


class TestFormatLivesOnTheFile:
    """A workspace is allowed to hold a thesis in TeX beside a shopping list in
    markdown. A preference would force every new document to be whatever the
    last one was."""

    def test_a_new_document_records_what_it_is(self, isolated_db):
        made = notes.create_note("Paper", "", doc_format=notes.FORMAT_LATEX)
        assert made["format"] == notes.FORMAT_LATEX
        assert notes.get_note(made["id"])["format"] == notes.FORMAT_LATEX

    def test_markdown_is_the_default(self, isolated_db):
        made = notes.create_note("Note")
        assert notes.get_note(made["id"])["format"] == notes.FORMAT_MARKDOWN

    def test_a_note_written_before_this_existed_is_markdown(self, isolated_db):
        """Which is the truth about every one of them — LaTeX documents had
        nowhere to record that they were LaTeX."""
        made = notes.create_note("Old")
        path = notes.get_note_path(made["id"])
        text = Path(path).read_text(encoding="utf-8").replace("format: markdown\n", "")
        Path(path).write_text(text, encoding="utf-8")
        assert notes.get_note(made["id"])["format"] == notes.FORMAT_MARKDOWN

    def test_editing_does_not_silently_change_the_format(self, isolated_db):
        """The frontmatter is rewritten on every save, so a key that is not
        carried through is a document that becomes markdown the first time
        somebody types in it."""
        made = notes.create_note("Paper", "x", doc_format=notes.FORMAT_LATEX)
        notes.update_note(made["id"], "\\section{One}")
        assert notes.get_note(made["id"])["format"] == notes.FORMAT_LATEX

    def test_nonsense_is_not_a_format(self, isolated_db):
        made = notes.create_note("Odd", "", doc_format="wingdings")
        assert notes.get_note(made["id"])["format"] == notes.FORMAT_MARKDOWN

    def test_the_listing_reports_it_too(self, isolated_db):
        notes.create_note("Paper", "", doc_format=notes.FORMAT_LATEX)
        assert [n["format"] for n in notes.list_notes()] == [notes.FORMAT_LATEX]

    def test_the_endpoint_accepts_it(self, client):
        made = client.post("/api/notes", json={"title": "Paper", "content": "",
                                               "format": "latex"}).json()
        assert made["format"] == "latex"


class TestTheCards:
    def test_every_kind_of_document_is_offered(self, client):
        """The exact list, in order, on purpose.

        This is the screen's whole content, so a card appearing or vanishing is
        a change somebody should have to write down. Blank stays first because
        it is what most people want and the eye starts there.
        """
        cards = client.get("/api/write/start").json()["cards"]
        assert [c["id"] for c in cards] == ["blank", "latex", "canvas", "slides"]

    def test_canvas_and_slides_need_no_pack(self, client):
        """Unlike LaTeX, neither depends on an extension being switched on, so
        neither can be offered in a state where opening it does nothing."""
        cards = {c["id"]: c for c in client.get("/api/write/start").json()["cards"]}
        assert cards["canvas"]["available"] is True
        assert cards["canvas"]["format"] == notes.FORMAT_CANVAS
        assert cards["slides"]["available"] is True
        assert cards["slides"]["format"] == notes.FORMAT_SLIDES

    def test_blank_is_always_available(self, client):
        cards = {c["id"]: c for c in client.get("/api/write/start").json()["cards"]}
        assert cards["blank"]["available"] is True
        assert cards["blank"]["format"] == "markdown"

    def test_latex_needs_the_academia_pack(self, client, monkeypatch):
        """A card that opens an editor whose validate and compile do nothing is
        worse than no card — the person finds out after they start writing."""
        from carrot import extensions

        monkeypatch.setattr(extensions, "is_enabled", lambda pack_id: False)
        cards = {c["id"]: c for c in client.get("/api/write/start").json()["cards"]}
        assert cards["latex"]["available"] is False

    def test_an_unavailable_card_says_what_to_switch_on(self, client, monkeypatch):
        """Rather than silently having one fewer option."""
        from carrot import extensions

        monkeypatch.setattr(extensions, "is_enabled", lambda pack_id: False)
        cards = {c["id"]: c for c in client.get("/api/write/start").json()["cards"]}
        assert "Academia" in cards["latex"]["requires"]["label"]
        assert "Extensions" in cards["latex"]["requires"]["detail"]

    def test_latex_is_available_when_the_pack_is_on(self, client, monkeypatch):
        from carrot import extensions

        monkeypatch.setattr(extensions, "is_enabled", lambda pack_id: True)
        cards = {c["id"]: c for c in client.get("/api/write/start").json()["cards"]}
        assert cards["latex"]["available"] is True

    def test_latex_is_its_own_card_not_a_chooser_behind_blank(self):
        """Markdown and LaTeX are different documents, not a setting on the
        same document, and making one a second click says the opposite."""
        js = read("js", "features.js")
        block = js[js.index("async function startNewDocument"):]
        assert "confirm(" not in block[:600] and "prompt(" not in block[:600]


class TestTheScreen:
    def test_write_opens_on_it_rather_than_an_empty_sentence(self):
        js = read("js", "features.js")
        assert "if (!currentNoteId) showWriteStart();" in js

    def test_it_has_somewhere_to_draw_cards_and_recents(self):
        html = read("index.html")
        assert 'id="write-start-cards"' in html
        assert 'id="write-start-recents"' in html

    def test_recents_are_newest_first(self):
        js = read("js", "features.js")
        block = js[js.index("function renderWriteStartRecents"):]
        assert "(b.created_at || 0) - (a.created_at || 0)" in block

    def test_opening_a_document_leaves_the_start_screen(self):
        js = read("js", "features.js")
        block = js[js.index("async function openNote"):js.index("async function mountEditor")]
        assert "hideWriteStart()" in block


class TestOpeningTheRightEditor:
    def test_a_latex_document_routes_to_the_latex_pane(self):
        js = read("js", "features.js")
        block = js[js.index("async function openNote"):js.index("async function mountEditor")]
        assert "note.format === 'latex'" in block
        assert "openLatexDoc" in block

    def test_the_latex_pane_can_open_an_existing_document(self):
        """It could make documents and could not open one."""
        js = read("js", "latexnote.js")
        assert "function openLatexDoc" in js

    def test_saving_updates_the_open_document_rather_than_making_another(self):
        """`saveLatexDoc` posted a new note every time, so editing yesterday's
        paper produced a second copy of it."""
        js = read("js", "latexnote.js")
        block = js[js.index("async function saveLatexDocSmart"):]
        assert "if (currentLatexNoteId)" in block
        assert "method: 'PUT'" in block

    def test_a_new_latex_document_forgets_the_last_one(self):
        js = read("js", "latexnote.js")
        block = js[js.index("function newLatexDoc"):]
        assert "currentLatexNoteId = null" in block[:400]

    def test_the_save_button_uses_it(self):
        assert "saveLatexDocSmart()" in read("index.html")
