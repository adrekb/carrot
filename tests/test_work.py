"""Work: one listing for everything you have.

Documents you wrote and files you pointed Carrot at are different rows in
different tables and the same thing on this screen — something with a name, a
kind and a date, that you are trying to find again.
"""
import json
import os

import pytest

from carrot import notes


class TestOneListing:
    def test_documents_appear(self, client, isolated_db):
        notes.create_note("Lecture 1", "body")
        items = client.get("/api/work/items").json()["items"]
        assert [i["name"] for i in items if i["kind"] == "document"] == ["Lecture 1"]

    def test_every_format_is_one_kind_of_thing(self, client, isolated_db):
        """A canvas and a deck are documents. The browser groups by `kind` and
        labels by `format`, so a canvas listed as its own kind would be a
        second thing to filter for that means the same as the first."""
        notes.create_note("Deck", "", doc_format=notes.FORMAT_SLIDES)
        notes.create_note("Board", "{}", doc_format=notes.FORMAT_CANVAS)
        items = client.get("/api/work/items").json()["items"]
        assert {i["format"] for i in items} == {"slides", "canvas"}
        assert {i["kind"] for i in items} == {"document"}

    def test_it_is_sorted_newest_first_across_both(self, client, isolated_db):
        """The point of merging server-side. Two lists that are each sorted
        interleave wrongly when the browser concatenates them.

        The mtimes are set apart deliberately. Three notes made in a loop land
        in the same second, and identical stamps satisfy "descending" whatever
        order they come back in — a test that passes with the sort deleted.
        """
        made = [notes.create_note(t, "") for t in ("One", "Two", "Three")]
        for offset, note in enumerate(made):
            stamp = 1_700_000_000 + offset * 3600
            os.utime(note["path"], (stamp, stamp))

        items = client.get("/api/work/items").json()["items"]
        assert [i["name"] for i in items] == ["Three", "Two", "One"]

    def test_a_file_timestamp_is_the_same_kind_of_number(self, client, isolated_db):
        """Documents carry epoch seconds and the index carries ISO text. Sorting
        one list on both compares a string to a number, which is NaN in the
        browser and a TypeError here — either way, not sorted."""
        notes.create_note("Something", "")
        items = client.get("/api/work/items").json()["items"]
        assert items, "nothing listed, so the assertion below would prove nothing"
        assert all(isinstance(i["updated"], (int, float)) for i in items)
        assert not any(isinstance(i["updated"], bool) for i in items)


class TestNarrowingIt:
    def test_by_text(self, client, isolated_db):
        notes.create_note("Lecture 1", "")
        notes.create_note("Shopping", "")
        items = client.get("/api/work/items?q=lect").json()["items"]
        assert [i["name"] for i in items] == ["Lecture 1"]

    def test_the_search_reads_the_body_too(self, client, isolated_db):
        notes.create_note("Untitled", "something about rationalism")
        items = client.get("/api/work/items?q=rationalism").json()["items"]
        assert len(items) == 1

    def test_by_format(self, client, isolated_db):
        notes.create_note("Deck", "", doc_format=notes.FORMAT_SLIDES)
        notes.create_note("Note", "")
        items = client.get("/api/work/items?kind=slides").json()["items"]
        assert [i["name"] for i in items] == ["Deck"]

    def test_asking_for_a_workspace_excludes_loose_files(self, client, isolated_db):
        """A file is not filed in a workspace. Showing every file under every
        workspace would make the filter mean nothing.

        Filed against a real workspace holding a real document, so the listing
        is non-empty — asked against a workspace that does not exist, `all()`
        over nothing is true and the filter could be missing entirely.
        """
        from carrot import workspaces

        made = workspaces.create_workspace("Thesis")
        note = notes.create_note("Chapter one", "")
        workspaces.file_item(workspaces.KIND_NOTE, note["id"], made["id"])

        items = client.get(f"/api/work/items?workspace={made['id']}").json()["items"]
        assert [i["name"] for i in items] == ["Chapter one"]
        assert all(i["kind"] != "file" for i in items)

    def test_the_search_reads_past_the_preview(self, client, isolated_db):
        """The tile shows 220 characters; the search must not stop there.

        Searching the preview makes a document findable by its first paragraph
        and invisible by its fourth, which is the wrong half of a long note —
        the part you cannot remember is exactly the part you search for.
        """
        buried = "x" * 900 + " pelican"
        notes.create_note("Long one", buried)
        items = client.get("/api/work/items?q=pelican").json()["items"]
        assert [i["name"] for i in items] == ["Long one"]

    def test_the_body_never_comes_back_with_the_listing(self, client, isolated_db):
        """The search reads whole documents; the response must not carry them.

        A grid of tiles needs a name, a date and 220 characters. Shipping every
        body so the browser can search would make listing the vault cost what
        the vault weighs.
        """
        notes.create_note("Long one", "y" * 5000)
        item = client.get("/api/work/items").json()["items"][0]
        assert "_haystack" not in item
        assert len(item["preview"]) <= 220

    def test_nothing_matching_is_not_an_error(self, client, isolated_db):
        body = client.get("/api/work/items?q=zzzznothing").json()
        assert body["items"] == [] and body["total"] == 0


class TestMakingSomewhereToPutThings:
    """The rail lists workspaces and had no way to add one.

    Everything else you can start is in the New menu; a workspace was the one
    thing that sent you to a different tab to make it, which is the shape the
    drive exists to get rid of.
    """

    def read(self, *parts):
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "carrot" / "web"
                ).joinpath(*parts).read_text(encoding="utf-8")

    def test_the_new_menu_offers_one(self):
        js = self.read("js", "features.js")
        assert 'data-card="__workspace"' in js

    def test_it_is_wired_to_something_that_exists(self):
        js = self.read("js", "features.js")
        assert "newDriveWorkspace()" in js
        assert "async function newDriveWorkspace" in js

    def test_it_does_not_use_a_prompt_electron_disables(self):
        """window.prompt returns null without showing anything in Electron, so
        the menu item would be a button that quietly does nothing."""
        js = self.read("js", "features.js")
        block = js[js.index("async function newDriveWorkspace"):]
        body = block[:block.index("\n}")]
        assert "prompt(" not in body.replace("inlineTextPrompt(", "")
        assert "inlineTextPrompt(" in body

    def test_a_workspace_is_named_rather_than_counted(self):
        """Standing in a workspace is being somewhere, not narrowing something.
        Counting it meant a workspace you had just made was headed "0 items"."""
        js = self.read("js", "features.js")
        assert "function drivePlaceName" in js
        assert "drivePlaceName()" in js

    def test_creating_one_lands_in_it(self, client, isolated_db):
        """The endpoint the menu calls has to give back the id, or there is
        nothing to select afterwards and you are told you made something while
        looking at everything you already had."""
        made = client.post("/api/workspaces",
                           json={"name": "Michaelmas term", "folder_id": None}).json()
        assert made.get("id")
        assert made.get("name") == "Michaelmas term"

        places = client.get("/api/work/places").json()["workspaces"]
        assert any(w["id"] == made["id"] and w["count"] == 0 for w in places)


class TestDeletingSeveral:
    """The one destructive thing on this screen.

    Selecting a hundred and sixty duplicates and pressing Delete is what it is
    for, which is also why the failure modes matter: a partial delete that
    reports success, or a selection that quietly takes Goals with it.
    """

    def test_it_deletes_what_was_asked_for(self, client, isolated_db):
        keep = notes.create_note("Keep", "")
        drop = [notes.create_note(f"Drop {n}", "") for n in range(3)]
        body = client.post("/api/work/delete",
                           json={"ids": [d["id"] for d in drop]}).json()
        assert body["deleted"] == 3 and body["skipped"] == 0
        left = client.get("/api/work/items").json()["items"]
        assert [i["name"] for i in left] == ["Keep"]
        assert notes.get_note(keep["id"]) is not None

    def test_a_system_document_is_refused(self, client, isolated_db):
        from carrot import systemdocs

        target = next(iter(systemdocs.SYSTEM_IDS))
        body = client.post("/api/work/delete", json={"ids": [target]}).json()
        assert body["deleted"] == 0 and body["skipped"] == 1
        assert target not in body["ids"]

    def test_one_bad_id_does_not_lose_the_rest(self, client, isolated_db):
        """The whole point of reporting per-document rather than raising: a
        selection that happens to include Goals should delete everything else
        and say what it skipped, not refuse all of it."""
        from carrot import systemdocs

        mine = notes.create_note("Mine", "")
        target = next(iter(systemdocs.SYSTEM_IDS))
        body = client.post("/api/work/delete",
                           json={"ids": [target, mine["id"], "no-such-note"]}).json()
        assert body["deleted"] == 1
        assert body["ids"] == [mine["id"]]
        assert body["skipped"] == 2
        assert notes.get_note(mine["id"]) is None

    def test_deleting_nothing_is_not_an_error(self, client, isolated_db):
        body = client.post("/api/work/delete", json={"ids": []}).json()
        assert body["deleted"] == 0 and body["skipped"] == 0


class TestThePlaces:
    def test_it_lists_workspaces_with_counts(self, client, isolated_db):
        from carrot import workspaces

        made = workspaces.create_workspace("Thesis")
        note = notes.create_note("Chapter one", "")
        workspaces.file_item(workspaces.KIND_NOTE, note["id"], made["id"])
        places = client.get("/api/work/places").json()["workspaces"]
        mine = next(w for w in places if w["id"] == made["id"])
        assert mine["name"] == "Thesis"
        assert mine["count"] == 1

    def test_an_empty_workspace_still_appears(self, client, isolated_db):
        """It is somewhere to file things into, so it has to be offered before
        anything is in it."""
        from carrot import workspaces

        made = workspaces.create_workspace("Empty")
        places = client.get("/api/work/places").json()["workspaces"]
        assert any(w["id"] == made["id"] and w["count"] == 0 for w in places)


class TestATileShowsTheDocument:
    """Every canvas drew the same three hard-coded shapes and every deck the
    same list of all its words, so a grid of them was a grid of identical
    pictures — you could read the badge and the filename and nothing else.

    The body cannot simply be sent: a listing that draws the whole drive would
    then weigh what the vault weighs, which is why `_work_preview` gives a
    canvas nothing at all. So the tile gets the *arrangement* — a bounded set
    of shapes in unit coordinates.
    """

    CANVAS = json.dumps({"type": "excalidraw", "elements": [
        {"type": "rectangle", "x": 60, "y": 80, "width": 220, "height": 120},
        {"type": "text", "x": 72, "y": 92, "width": 196, "height": 24,
         "text": "Rationalism\nsecond line"},
        {"type": "arrow", "x": 300, "y": 200, "width": 100, "height": 60},
    ]})

    def _thumb(self, client, name, body, fmt):
        notes.create_note(name, body, doc_format=fmt)
        items = client.get("/api/work/items").json()["items"]
        return next(i for i in items if i["name"] == name)["thumb"]

    def test_a_canvas_carries_its_arrangement(self, client, isolated_db):
        thumb = self._thumb(client, "Board", self.CANVAS, notes.FORMAT_CANVAS)
        assert thumb["kind"] == "canvas"
        assert [s["t"] for s in thumb["shapes"]] == ["box", "text", "line"]
        # Unit coordinates against the bounding box of everything on it, so a
        # canvas whose shapes sit far from the origin still looks like itself.
        assert thumb["shapes"][0]["x"] == 0 and thumb["shapes"][0]["y"] == 0
        assert all(0 <= s["x"] <= 1 and 0 <= s["y"] <= 1 for s in thumb["shapes"])

    def test_canvas_text_is_one_line_not_the_paragraph(self, client, isolated_db):
        thumb = self._thumb(client, "Board2", self.CANVAS, notes.FORMAT_CANVAS)
        text = next(s for s in thumb["shapes"] if s["t"] == "text")
        assert text["s"] == "Rationalism"

    def test_an_empty_canvas_says_so_rather_than_drawing_nothing(self, client, isolated_db):
        thumb = self._thumb(client, "Blank", '{"elements": []}', notes.FORMAT_CANVAS)
        assert thumb == {"kind": "canvas", "shapes": [], "more": 0}

    def test_a_deck_gets_its_first_slide_not_every_word(self, client, isolated_db):
        deck = json.dumps({"size": {"w": 1280, "h": 720}, "slides": [
            {"elements": [
                {"type": "text", "x": 96, "y": 96, "w": 1088, "h": 120,
                 "text": "Your deck", "size": 56, "bold": True},
                {"type": "text", "x": 96, "y": 250, "w": 1088, "h": 380,
                 "text": "A subtitle", "size": 30},
            ]},
            {"elements": [{"type": "text", "x": 0, "y": 0, "w": 100, "h": 50,
                           "text": "Slide two", "size": 40}]},
        ]})
        thumb = self._thumb(client, "Deck", deck, notes.FORMAT_SLIDES)
        assert thumb["count"] == 2
        assert [s.get("s") for s in thumb["shapes"]] == ["Your deck", "A subtitle"]

    def test_a_slide_is_measured_against_the_slide_not_its_contents(self, client, isolated_db):
        """A decoration hanging off the left edge — the shipped template parks
        one at x=-228 — would otherwise drag the title inward and report a
        layout the slide does not have."""
        deck = json.dumps({"size": {"w": 1280, "h": 720}, "slides": [{"elements": [
            {"type": "text", "x": 96, "y": 96, "w": 1088, "h": 120, "text": "Title", "size": 56},
            {"type": "chevron", "x": -228, "y": 58, "w": 320, "h": 240},
        ]}]})
        thumb = self._thumb(client, "Deck2", deck, notes.FORMAT_SLIDES)
        title = next(s for s in thumb["shapes"] if s.get("s") == "Title")
        assert title["x"] == pytest.approx(96 / 1280, abs=0.001)
        # The decoration keeps its true position, outside the frame, and is
        # clipped when drawn rather than being allowed to move the slide.
        assert min(s["x"] for s in thumb["shapes"]) < 0

    def test_type_size_comes_from_the_element_not_its_box(self, client, isolated_db):
        """A text element's height is the frame it may grow into. Sizing from
        that set a 30pt subtitle in a tall box larger than a 56pt title in a
        short one, and the tile rendered upside down."""
        deck = json.dumps({"size": {"w": 1280, "h": 720}, "slides": [{"elements": [
            {"type": "text", "x": 96, "y": 96, "w": 1088, "h": 120, "text": "Big", "size": 56},
            {"type": "text", "x": 96, "y": 250, "w": 1088, "h": 380, "text": "Small", "size": 30},
        ]}]})
        thumb = self._thumb(client, "Deck3", deck, notes.FORMAT_SLIDES)
        big = next(s for s in thumb["shapes"] if s["s"] == "Big")
        small = next(s for s in thumb["shapes"] if s["s"] == "Small")
        assert big["fs"] > small["fs"]
        assert big["h"] < small["h"]     # the box ordering that misled it

    def test_a_tile_is_bounded_however_big_the_document(self, client, isolated_db):
        """Hundreds of shapes stop being distinguishable at tile size and cost
        bytes in a listing that draws everything."""
        many = json.dumps({"elements": [
            {"type": "rectangle", "x": i * 10, "y": i * 10, "width": 5, "height": 5}
            for i in range(200)]})
        thumb = self._thumb(client, "Huge", many, notes.FORMAT_CANVAS)
        assert len(thumb["shapes"]) == 18
        assert thumb["more"] == 182

    def test_prose_has_no_geometry_because_its_preview_is_readable(self, client, isolated_db):
        thumb = self._thumb(client, "Essay", "# Heading\n\nText.", notes.FORMAT_MARKDOWN)
        assert thumb is None

    def test_a_body_that_will_not_parse_does_not_break_the_listing(self, client, isolated_db):
        assert self._thumb(client, "Broken", "not json", notes.FORMAT_CANVAS) is None
