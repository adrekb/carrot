"""Making a slide tidy, rather than making one.

The editor could put a thing on a slide and could not line two of them up. That
sounds like polish and is not: the eye catches a four-pixel disagreement
between two headings faster than it reads either of them, so "close enough" is
most of what makes a deck look homemade.

Three things fix it, and they are one feature — none of them means anything
without picking more than one element. These are source-level tests, in the
style of the rest of the web suite: there is no DOM here, so what can be
checked is that the operations exist, that they are wired to something, and
that the arithmetic decisions are the ones intended.
"""
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
      / "slides.js").read_text(encoding="utf-8")
CSS = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "css"
       / "style.css").read_text(encoding="utf-8")


def block(name):
    """One function's body, for asserting about it without matching the rest
    of a two-thousand-line file."""
    start = JS.index(f"function {name}")
    return JS[start:JS.index("\n}", start)]


class TestPickingMoreThanOne:
    def test_there_is_a_set_as_well_as_the_single_selection(self):
        """Kept beside `slidesSelected` rather than replacing it: every control
        that sets a font or a colour acts on one element, and rewriting all of
        them to take a set would be a much larger change for no gain."""
        assert "let slidesPicked = new Set()" in JS
        assert "let slidesSelected = null" in JS

    def test_a_modifier_adds_to_the_selection(self):
        body = block("bindSlidesEvents")
        assert "shiftKey" in body
        assert "metaKey" in body and "ctrlKey" in body

    def test_the_stage_highlights_everything_picked(self):
        """Not just the one the format bar is aimed at — a group where only one
        member looks selected is a group you cannot see."""
        assert "slidesPicked.has(el.id)" in block("renderSlideStage")

    def test_pressing_a_member_of_a_group_keeps_the_group(self):
        """That is how a row gets picked up by one of its members. If the press
        turns out to be a click rather than a drag it meant "just this one",
        which is settled on mouseup when which it was is known — without it
        there is no way back to one element except clicking empty space."""
        body = block("bindSlidesEvents")
        assert "collapseTo" in body

    def test_delete_takes_all_of_them(self):
        """Pressing Delete with four ticked and losing one is a half-obeyed
        instruction you have to undo and redo by hand."""
        body = block("deleteSlideElement")
        assert "slidesPicked.has(e.id)" in body
        assert "!slidesPicked.size" in body

    def test_there_is_a_way_to_pick_everything_and_a_way_out(self):
        body = block("bindSlidesEvents")
        assert "'a'" in body and "Escape" in body


class TestAligning:
    def test_all_six_edges_are_offered(self):
        for how in ("left", "centre", "right", "top", "middle", "bottom"):
            assert f"{how}:" in JS or f"'{how}'" in JS, how
        assert "SLIDE_ALIGNMENTS" in JS

    def test_it_moves_and_never_resizes(self):
        """Stretching a heading to match a box is the version of this that
        quietly ruins type."""
        start = JS.index("const SLIDE_ALIGNMENTS")
        body = JS[start:JS.index("};", start)]
        assert "el.w =" not in body and "el.h =" not in body

    def test_one_element_aligns_to_the_slide(self):
        """With a row selected somebody wants the row tidy; with one selected
        there is nothing to be tidy against except the slide, and refusing
        would make it a button that does nothing."""
        body = block("alignPicked")
        assert "SLIDE_W" in body and "SLIDE_H" in body
        assert "els.length > 1" in body

    def test_it_is_undoable(self):
        assert "pushSlidesHistory()" in block("alignPicked")


class TestDistributing:
    def test_it_needs_three(self):
        """With two there is one gap, and one gap is already even."""
        assert "els.length < 3" in block("distributePicked")

    def test_it_equalises_gaps_rather_than_centres(self):
        """Spacing centres evenly is the easier sum and the wrong one: with a
        wide box between two narrow ones it leaves visibly different gaps."""
        body = block("distributePicked")
        assert "size(el)" in body, "the gap has to account for each element's own width"
        assert "used" in body and "span" in body

    def test_the_ends_stay_put(self):
        body = block("distributePicked")
        assert "pos(first)" in body

    def test_it_works_on_both_axes(self):
        body = block("distributePicked")
        assert "axis === 'x'" in body


class TestSnapping:
    def test_the_slide_centre_is_a_target(self):
        """A title that is nearly centred is the single most common way a deck
        looks wrong."""
        body = block("snapTargets")
        assert "SLIDE_W / 2" in body and "SLIDE_H / 2" in body

    def test_other_elements_are_targets_and_the_dragged_ones_are_not(self):
        body = block("snapTargets")
        assert "ignore.has(el.id)" in body

    def test_it_offers_from_all_three_of_its_own_edges(self):
        """So a box snaps by whichever of its sides is nearest something, not
        only by the corner being dragged."""
        body = block("snapOffset")
        for edge in ("box.left", "box.cx", "box.right", "box.top", "box.cy", "box.bottom"):
            assert edge in body, edge

    def test_the_threshold_is_constant_on_screen(self):
        """At 40% zoom a 7px stage threshold is 3 real pixels, which is not a
        snap anyone can feel."""
        body = block("snapOffset")
        assert "SNAP_DISTANCE / (scale || 1)" in body

    def test_it_can_be_escaped(self):
        """A snap you cannot get out of is worse than no snap when the thing
        you want is deliberately off-grid."""
        assert "altKey" in block("bindSlidesEvents")

    def test_the_guides_are_cleared_on_release(self):
        """A guide that lingers is indistinguishable from something you drew."""
        assert "renderSnapGuides(null, null)" in block("bindSlidesEvents")

    def test_the_guides_are_styled(self):
        for cls in (".slide-guides", ".slide-guide-v", ".slide-guide-h"):
            assert cls in CSS, f"{cls} is drawn by renderSnapGuides but never styled"


class TestTheToolbar:
    def test_arrange_is_present_and_wired(self):
        body = block("renderSlideFormatBar")
        assert "alignPicked(" in body
        assert "distributePicked(" in body

    def test_distribute_is_disabled_rather_than_hidden_below_three(self):
        """So the row does not change width as the selection grows and the
        buttons stay where the hand learned they were."""
        body = block("renderSlideFormatBar")
        assert "picked.length < 3" in body
        assert "disabled" in body
        assert ".fmt-group .fmt-btn:disabled" in CSS

    def test_it_says_how_many_are_selected(self):
        assert "fmt-count" in block("renderSlideFormatBar")
        assert ".fmt-count" in CSS


class TestNothingElseMoved:
    def test_resize_is_still_one_element(self):
        """Scaling a mixed selection by a corner needs a decision per element
        about text size and aspect that nothing here can make well, and
        guessing it wrong ruins the slide."""
        body = block("bindSlidesEvents")
        assert "resizing ? [el] : pickedElements()" in body

    def test_selecting_does_not_enter_the_undo_history(self):
        """An empty step you have to press undo twice to get past."""
        assert "if (!moved)" in block("bindSlidesEvents")

    def test_every_reset_settles_both(self):
        """`slidesSelected` and `slidesPicked` disagreeing means the stage
        highlights something the format bar is not pointed at."""
        import re

        # Bare assignments are allowed only inside the helpers that own the
        # pair, plus the declaration itself.
        owners = ("function pickOnly", "function togglePicked", "function bindSlidesEvents")
        for match in re.finditer(r"^\s*slidesSelected = (?!null;?$)", JS, re.MULTILINE):
            preceding = JS[:match.start()]
            nearest = max((preceding.rfind(o) for o in owners), default=-1)
            assert nearest >= 0, "a selection is set outside the helpers that own the pair"
