"""Applying an edit to a deck or a canvas, as operations against the JSON.

`doctext` renders a document so a model can read it and made every element
addressable — `[2] on slide 1` — precisely so an edit could name a part rather
than replace the whole. This is the half that acts on those addresses.

The tests that matter most here are not the ones proving an edit lands. They
are the ones proving an edit lands on the element the *model meant*, when the
same batch also deletes something before it, and the ones proving a batch that
cannot be finished changes nothing at all. Both failure modes are silent: the
document still parses, the editor still opens it, and the only symptom is a
slide that says something nobody wrote.
"""
import json
import re
from pathlib import Path

import pytest

from carrot import docedit, doctext
from carrot.docedit import DocEditError, apply_operations


# A deterministic id, so a test can assert on a whole document.
def ids():
    counter = {"n": 0}

    def mint(prefix):
        counter["n"] += 1
        # "new" keeps a minted id from ever colliding with a fixture's s1/e0,
        # so a test asserting an element is the fresh one cannot pass by
        # accident.
        return f"{prefix}new{counter['n']}"
    return mint


def element(index, **extra):
    base = {"id": f"e{index}", "type": "text", "text": f"element {index}",
            "x": 100, "y": 100, "w": 400, "h": 80}
    base.update(extra)
    return base


def slide(*elements, id="s1", notes=""):
    return {"id": id, "background": "", "elements": list(elements), "notes": notes}


def deck(*slides, **extra):
    body = {"type": "carrot-slides", "version": 1, "slides": list(slides)}
    body.update(extra)
    return json.dumps(body)


def applied(body, ops, fmt=doctext.FORMAT_SLIDES):
    return json.loads(apply_operations(body, fmt, ops, new_id=ids()))


# ===== The addressing rule =====
# The reason this module resolves every address before it applies anything.

class TestAddressesMeanWhatTheModelSaw:

    def test_a_delete_does_not_shift_a_later_address(self):
        """The bug this module is shaped to prevent.

        The model read three elements and asked to delete the first and retitle
        the third. Applied in order against a shrinking list, "element 2" would
        land on what was element 3 — a wrong edit that still parses, still
        renders, and reports nothing.
        """
        body = deck(slide(element(0), element(1), element(2), element(3)))
        out = applied(body, [
            {"op": "delete_element", "slide": 1, "element": 0},
            {"op": "set_text", "slide": 1, "element": 2, "text": "retitled"},
        ])
        left = out["slides"][0]["elements"]
        assert [e["id"] for e in left] == ["e1", "e2", "e3"]
        # e2 is the element the model named. Applied against a list already
        # shortened by the delete, "element 2" would have been e3 — a wrong
        # edit that raises nothing and reads as a success.
        assert [e["text"] for e in left] == ["element 1", "retitled", "element 3"]

    def test_two_deletes_both_land_on_what_was_named(self):
        body = deck(slide(element(0), element(1), element(2), element(3)))
        out = applied(body, [
            {"op": "delete_element", "slide": 1, "element": 0},
            {"op": "delete_element", "slide": 1, "element": 2},
        ])
        assert [e["id"] for e in out["slides"][0]["elements"]] == ["e1", "e3"]

    def test_a_deleted_slide_does_not_shift_a_later_slide_number(self):
        body = deck(slide(element(0), id="s1"),
                    slide(element(0), id="s2"),
                    slide(element(0), id="s3"))
        out = applied(body, [
            {"op": "delete_slide", "slide": 1},
            {"op": "set_text", "slide": 3, "element": 0, "text": "third"},
        ])
        assert [s["id"] for s in out["slides"]] == ["s2", "s3"]
        assert out["slides"][1]["elements"][0]["text"] == "third"

    def test_an_unreadable_element_still_occupies_its_index(self):
        """`deck_as_text` renders a non-dict element as "[i] (unreadable
        element)" and numbers it, so it holds a place in the addressing. Passing
        over it here would shift every address after it by one."""
        body = deck(slide("not an element", element(1), element(2)))
        # The rendering the model reads agrees the junk is [0].
        rendered = doctext.deck_as_text(body)
        assert "[0] (unreadable element)" in rendered

        out = applied(body, [{"op": "set_text", "slide": 1, "element": 1, "text": "hit"}])
        left = out["slides"][0]["elements"]
        assert left[0] == "not an element"
        assert left[1]["text"] == "hit"
        assert left[2]["text"] == "element 2"

    def test_an_unreadable_element_cannot_itself_be_edited(self):
        body = deck(slide("not an element", element(1)))
        with pytest.raises(DocEditError, match="not a readable element"):
            apply_operations(body, doctext.FORMAT_SLIDES,
                             [{"op": "set_text", "slide": 1, "element": 0, "text": "x"}],
                             new_id=ids())

    def test_editing_something_an_earlier_op_deleted_is_refused(self):
        """Not silently skipped: the model asked for two things and got one."""
        body = deck(slide(element(0), element(1)))
        with pytest.raises(DocEditError, match="deleted the element"):
            apply_operations(body, doctext.FORMAT_SLIDES, [
                {"op": "delete_element", "slide": 1, "element": 1},
                {"op": "set_text", "slide": 1, "element": 1, "text": "gone"},
            ], new_id=ids())

    def test_editing_a_slide_an_earlier_op_deleted_is_refused(self):
        body = deck(slide(element(0), id="s1"), slide(element(0), id="s2"))
        with pytest.raises(DocEditError, match="deleted the slide"):
            apply_operations(body, doctext.FORMAT_SLIDES, [
                {"op": "delete_slide", "slide": 2},
                {"op": "set_notes", "slide": 2, "notes": "x"},
            ], new_id=ids())


# ===== All or nothing =====

class TestABatchIsAllOrNothing:

    def test_a_bad_operation_late_in_a_batch_applies_none_of_it(self):
        body = deck(slide(element(0), element(1)))
        with pytest.raises(DocEditError):
            apply_operations(body, doctext.FORMAT_SLIDES, [
                {"op": "set_text", "slide": 1, "element": 0, "text": "changed"},
                {"op": "set_text", "slide": 1, "element": 9, "text": "nope"},
            ], new_id=ids())
        # The document is what it was — the good first operation did not land.
        assert json.loads(body)["slides"][0]["elements"][0]["text"] == "element 0"

    def test_the_input_string_is_never_mutated(self):
        body = deck(slide(element(0)))
        before = body
        apply_operations(body, doctext.FORMAT_SLIDES,
                         [{"op": "set_text", "slide": 1, "element": 0, "text": "new"}],
                         new_id=ids())
        assert body == before

    def test_no_operations_returns_the_body_unchanged(self):
        body = deck(slide(element(0)))
        assert apply_operations(body, doctext.FORMAT_SLIDES, []) == body


# ===== What survives an edit =====

class TestNothingElseIsDropped:

    def test_the_decks_own_keys_survive(self):
        """`type` and `version` sit beside `slides` in a saved deck."""
        body = deck(slide(element(0)))
        out = applied(body, [{"op": "set_text", "slide": 1, "element": 0, "text": "x"}])
        assert out["type"] == "carrot-slides"
        assert out["version"] == 1

    def test_an_elements_styling_survives_a_text_change(self):
        """The whole reason a deck is not edited by rewriting its text."""
        styled = element(0, fill="#ff0000", bold=True, size=64, align="center")
        body = deck(slide(styled))
        out = applied(body, [{"op": "set_text", "slide": 1, "element": 0, "text": "new"}])
        kept = out["slides"][0]["elements"][0]
        assert kept["text"] == "new"
        assert (kept["fill"], kept["bold"], kept["size"], kept["align"]) == \
               ("#ff0000", True, 64, "center")

    def test_an_image_element_keeps_its_source(self):
        img = {"id": "e0", "type": "image", "src": "data:image/png;base64,AAAA",
               "x": 0, "y": 0, "w": 10, "h": 10}
        body = deck(slide(img, element(1)))
        out = applied(body, [{"op": "set_text", "slide": 1, "element": 1, "text": "x"}])
        assert out["slides"][0]["elements"][0]["src"] == "data:image/png;base64,AAAA"

    def test_an_unknown_key_on_a_slide_survives(self):
        body = deck({"id": "s1", "elements": [element(0)], "transition": "fade"})
        out = applied(body, [{"op": "set_text", "slide": 1, "element": 0, "text": "x"}])
        assert out["slides"][0]["transition"] == "fade"


# ===== The deck operations =====

class TestDeckOperations:

    def test_set_text(self):
        body = deck(slide(element(0)))
        out = applied(body, [{"op": "set_text", "slide": 1, "element": 0, "text": "Postgres"}])
        assert out["slides"][0]["elements"][0]["text"] == "Postgres"

    def test_set_notes(self):
        body = deck(slide(element(0)))
        out = applied(body, [{"op": "set_notes", "slide": 1, "notes": "mention the budget"}])
        assert out["slides"][0]["notes"] == "mention the budget"

    def test_add_element_carries_the_editors_defaults(self):
        """An element missing these is one the editor has to guess about."""
        body = deck(slide())
        out = applied(body, [{"op": "add_element", "slide": 1, "type": "cylinder",
                              "text": "Store", "x": 580, "y": 200, "w": 320, "h": 240}])
        added = out["slides"][0]["elements"][0]
        assert added["type"] == "cylinder"
        assert added["text"] == "Store"
        assert (added["x"], added["y"], added["w"], added["h"]) == (580, 200, 320, 240)
        # Everything makeElement sets, set.
        for key in docedit.DECK_ELEMENT_DEFAULTS:
            assert key in added
        assert added["id"]

    def test_add_element_without_a_position_still_has_one(self):
        body = deck(slide())
        out = applied(body, [{"op": "add_element", "slide": 1, "type": "rect"}])
        added = out["slides"][0]["elements"][0]
        assert (added["x"], added["y"]) == (160, 160)

    def test_delete_element(self):
        body = deck(slide(element(0), element(1)))
        out = applied(body, [{"op": "delete_element", "slide": 1, "element": 0}])
        assert [e["id"] for e in out["slides"][0]["elements"]] == ["e1"]

    def test_move_element_changes_only_what_it_names(self):
        body = deck(slide(element(0)))
        out = applied(body, [{"op": "move_element", "slide": 1, "element": 0, "x": 20}])
        moved = out["slides"][0]["elements"][0]
        assert moved["x"] == 20
        assert (moved["y"], moved["w"], moved["h"]) == (100, 400, 80)

    def test_add_slide_at_a_position(self):
        body = deck(slide(id="s1"), slide(id="s2"))
        out = applied(body, [{"op": "add_slide", "at": 2}])
        # Slide numbers are 1-based, so "at 2" puts it between the two.
        assert len(out["slides"]) == 3
        assert out["slides"][0]["id"] == "s1"
        assert out["slides"][2]["id"] == "s2"
        assert out["slides"][1]["elements"] == []
        assert out["slides"][1]["id"] not in ("s1", "s2")

    def test_add_slide_without_a_position_appends(self):
        body = deck(slide(id="s1"), slide(id="s2"))
        out = applied(body, [{"op": "add_slide"}])
        assert len(out["slides"]) == 3
        assert [s["id"] for s in out["slides"][:2]] == ["s1", "s2"]

    def test_delete_slide(self):
        body = deck(slide(id="s1"), slide(id="s2"))
        out = applied(body, [{"op": "delete_slide", "slide": 1}])
        assert [s["id"] for s in out["slides"]] == ["s2"]

    def test_the_last_slide_cannot_be_deleted(self):
        """A deck with no slides is not a deck, and the editor has nothing to show."""
        body = deck(slide(element(0)))
        with pytest.raises(DocEditError, match="only slide"):
            apply_operations(body, doctext.FORMAT_SLIDES,
                             [{"op": "delete_slide", "slide": 1}], new_id=ids())

    def test_a_batch_cannot_delete_every_slide(self):
        """Each delete is judged against the deck as it was — so all three
        look survivable alone and together leave nothing to open."""
        body = deck(slide(id="sA"), slide(id="sB"), slide(id="sC"))
        with pytest.raises(DocEditError, match="every slide"):
            apply_operations(body, doctext.FORMAT_SLIDES, [
                {"op": "delete_slide", "slide": 1},
                {"op": "delete_slide", "slide": 2},
                {"op": "delete_slide", "slide": 3},
            ], new_id=ids())

    def test_a_batch_may_delete_all_but_one(self):
        body = deck(slide(id="sA"), slide(id="sB"), slide(id="sC"))
        out = applied(body, [
            {"op": "delete_slide", "slide": 1},
            {"op": "delete_slide", "slide": 3},
        ])
        assert [s["id"] for s in out["slides"]] == ["sB"]


# ===== What a model gets told when it is wrong =====

class TestRefusalsSayWhatWasAvailable:

    def test_a_slide_out_of_range_names_how_many_there_are(self):
        body = deck(slide(element(0)))
        with pytest.raises(DocEditError, match="no slide 4.*has 1 slide"):
            apply_operations(body, doctext.FORMAT_SLIDES,
                             [{"op": "set_text", "slide": 4, "element": 0, "text": "x"}],
                             new_id=ids())

    def test_an_element_out_of_range_names_how_many_there_are(self):
        body = deck(slide(element(0), element(1)))
        with pytest.raises(DocEditError, match=r"no element \[7\].*has 2 elements"):
            apply_operations(body, doctext.FORMAT_SLIDES,
                             [{"op": "set_text", "slide": 1, "element": 7, "text": "x"}],
                             new_id=ids())

    def test_an_unknown_operation_lists_the_ones_that_exist(self):
        body = deck(slide(element(0)))
        with pytest.raises(DocEditError, match="set_text"):
            apply_operations(body, doctext.FORMAT_SLIDES,
                             [{"op": "rewrite_everything", "slide": 1}], new_id=ids())

    def test_an_unknown_shape_lists_the_ones_that_exist(self):
        body = deck(slide())
        with pytest.raises(DocEditError, match="cylinder"):
            apply_operations(body, doctext.FORMAT_SLIDES,
                             [{"op": "add_element", "slide": 1, "type": "octagon2"}],
                             new_id=ids())

    def test_an_image_has_no_text_to_set(self):
        img = {"id": "e0", "type": "image", "src": "data:image/png;base64,A"}
        body = deck(slide(img))
        with pytest.raises(DocEditError, match="is an image"):
            apply_operations(body, doctext.FORMAT_SLIDES,
                             [{"op": "set_text", "slide": 1, "element": 0, "text": "x"}],
                             new_id=ids())

    def test_a_deck_still_in_markdown_says_so(self):
        """Decks convert on open; one that never opened is prose on disk."""
        with pytest.raises(DocEditError, match="not JSON yet"):
            apply_operations("# Just a heading\n", doctext.FORMAT_SLIDES,
                             [{"op": "set_text", "slide": 1, "element": 0, "text": "x"}])

    def test_a_prose_format_is_not_edited_by_operation(self):
        with pytest.raises(DocEditError, match="edited as text"):
            apply_operations("hello", doctext.FORMAT_MARKDOWN,
                             [{"op": "set_text", "slide": 1, "element": 0, "text": "x"}])

    def test_true_is_not_an_element_index(self):
        """bool is an int in Python, and `element: true` is a malformed op."""
        body = deck(slide(element(0)))
        with pytest.raises(DocEditError, match="element index"):
            apply_operations(body, doctext.FORMAT_SLIDES,
                             [{"op": "set_text", "slide": 1, "element": True, "text": "x"}],
                             new_id=ids())

    def test_a_move_that_moves_nothing_is_refused(self):
        body = deck(slide(element(0)))
        with pytest.raises(DocEditError, match="at least one of"):
            apply_operations(body, doctext.FORMAT_SLIDES,
                             [{"op": "move_element", "slide": 1, "element": 0}],
                             new_id=ids())


# ===== Canvases =====

def scene(*elements, bare=False):
    if bare:
        return json.dumps(list(elements))
    return json.dumps({"elements": list(elements), "appState": {"theme": "dark"}})


def shape(id, **extra):
    base = {"id": id, "type": "rectangle", "x": 0, "y": 0, "width": 100,
            "height": 100, "isDeleted": False, "version": 3, "versionNonce": 1}
    base.update(extra)
    return base


class TestCanvases:

    def test_an_address_skips_tombstones_the_way_the_rendering_does(self):
        """The subtle one.

        Excalidraw keeps deleted elements in the scene with `isDeleted` set,
        and `canvas_as_text` numbers only what is left. So `[1]` is the second
        *live* element. Counting raw array positions here would edit a shape
        the user cannot see, and the rendering would still say the edit landed
        somewhere else.
        """
        body = scene(shape("a"), shape("gone", isDeleted=True), shape("b"))
        # The rendering the model would have read agrees about the numbering.
        rendered = doctext.canvas_as_text(body)
        assert "[1]" in rendered and "gone" not in rendered

        out = json.loads(apply_operations(body, doctext.FORMAT_CANVAS,
                                          [{"op": "delete_element", "element": 1}],
                                          new_id=ids()))
        by_id = {e["id"]: e for e in out["elements"]}
        assert by_id["b"]["isDeleted"] is True
        assert by_id["a"]["isDeleted"] is False

    def test_delete_tombstones_rather_than_removes(self):
        """Excalidraw reconciles by merging; an element merely missing comes back."""
        body = scene(shape("a"), shape("b"))
        out = json.loads(apply_operations(body, doctext.FORMAT_CANVAS,
                                          [{"op": "delete_element", "element": 0}],
                                          new_id=ids()))
        assert len(out["elements"]) == 2
        assert out["elements"][0]["isDeleted"] is True

    def test_an_edit_bumps_the_version_so_reconciliation_takes_it(self):
        body = scene(shape("a", type="text", text="before", version=3))
        out = json.loads(apply_operations(body, doctext.FORMAT_CANVAS,
                                          [{"op": "set_text", "element": 0, "text": "after"}],
                                          new_id=ids()))
        assert out["elements"][0]["version"] == 4

    def test_set_text_keeps_original_text_in_step(self):
        """Excalidraw wraps text and keeps the unwrapped copy; setting one reverts."""
        body = scene(shape("a", type="text", text="before", originalText="before"))
        out = json.loads(apply_operations(body, doctext.FORMAT_CANVAS,
                                          [{"op": "set_text", "element": 0, "text": "after"}],
                                          new_id=ids()))
        assert out["elements"][0]["originalText"] == "after"

    def test_move_uses_excalidraws_field_names(self):
        """A canvas has width/height where a deck has w/h."""
        body = scene(shape("a"))
        out = json.loads(apply_operations(body, doctext.FORMAT_CANVAS,
                                          [{"op": "move_element", "element": 0,
                                            "x": 50, "w": 300}], new_id=ids()))
        moved = out["elements"][0]
        assert (moved["x"], moved["width"]) == (50, 300)
        assert "w" not in moved

    def test_a_bare_list_scene_stays_a_bare_list(self):
        body = scene(shape("a"), bare=True)
        out = json.loads(apply_operations(body, doctext.FORMAT_CANVAS,
                                          [{"op": "move_element", "element": 0, "x": 5}],
                                          new_id=ids()))
        assert isinstance(out, list)

    def test_the_scenes_app_state_survives(self):
        body = scene(shape("a"))
        out = json.loads(apply_operations(body, doctext.FORMAT_CANVAS,
                                          [{"op": "move_element", "element": 0, "x": 5}],
                                          new_id=ids()))
        assert out["appState"] == {"theme": "dark"}

    def test_a_shape_with_no_text_says_a_label_is_a_bound_element(self):
        body = scene(shape("a"))
        with pytest.raises(DocEditError, match="bound element"):
            apply_operations(body, doctext.FORMAT_CANVAS,
                             [{"op": "set_text", "element": 0, "text": "x"}], new_id=ids())

    def test_adding_to_a_canvas_is_not_offered(self):
        """Deliberate: a labelled Excalidraw shape is two elements bound by id."""
        body = scene(shape("a"))
        with pytest.raises(DocEditError, match="not an operation"):
            apply_operations(body, doctext.FORMAT_CANVAS,
                             [{"op": "add_element", "type": "rectangle"}], new_id=ids())


# ===== The two lists that must not drift =====

class TestTheShapeListMatchesTheEditor:

    def test_every_shape_the_editor_offers_can_be_added(self):
        """A shape added to slides.js and not here is silently rendered as a rect.

        The registry is duplicated in Python rather than parsed at import time,
        so this is what holds the copy honest.
        """
        source = (Path(__file__).resolve().parents[1]
                  / "carrot" / "web" / "js" / "slides.js").read_text(encoding="utf-8")
        block = re.search(r"const SLIDE_SHAPES = \{(.*?)\n\};", source, re.S)
        assert block, "SLIDE_SHAPES is not where this test expects it in slides.js"
        in_editor = set(re.findall(r"^\s{4}(\w+):\s*\{", block.group(1), re.M))
        assert in_editor, "no shapes parsed out of SLIDE_SHAPES"
        assert in_editor == set(docedit.DECK_SHAPES)
