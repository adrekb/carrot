"""The deck editor's controls, and the shapes it can draw.

Two separate things, both asked for as "make sure all buttons work".

The buttons do work — every handler in the slides pane resolves, and the audit
that found that is now the first class here so it stays true. What did not work
was the *library*: eleven shapes is enough to draw a box and an arrow and
nothing else. You reach for a cylinder for a database, a callout for a remark,
a left arrow for a flow that goes back, and there is a rectangle.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
SLIDES_JS = (WEB / "js" / "slides.js").read_text(encoding="utf-8")
CSS = (WEB / "css" / "style.css").read_text(encoding="utf-8")


def js_sources():
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted((WEB / "js").glob("*.js")))


def slides_pane():
    start = INDEX.index('id="slides-pane"')
    end = INDEX.index('id="graph-pane"') if 'id="graph-pane"' in INDEX else len(INDEX)
    return INDEX[start:end]


@pytest.fixture(scope="module")
def all_js():
    return js_sources()


class TestEveryButtonInTheDeckEditorResolves:
    """A handler that does not exist is a button that looks identical to one
    that does and throws into the console when pressed. This is the audit as a
    test: the names in the markup and the functions in the bundle, compared."""

    @pytest.mark.parametrize("handler", [
        "showWriteStart", "addSlideElement", "toggleShapeMenu", "pickSlideImage",
        "toggleSlidesExport", "exportDeckHtml", "presentDeck",
        "addSlide", "duplicateSlide", "deleteSlide",
    ])
    def test_the_handler_is_defined(self, handler, all_js):
        assert re.search(rf"function {handler}\b", all_js), f"{handler} is not defined"

    def test_no_onclick_in_the_pane_names_a_missing_function(self, all_js):
        """The general form, so a button added later is covered without anyone
        remembering to add it to the list above."""
        called = set(re.findall(r'onclick="(\w+)\(', slides_pane()))
        missing = [name for name in sorted(called)
                   if not re.search(rf"function {name}\b", all_js)]
        assert not missing, f"buttons with no handler: {missing}"

    def test_the_pane_still_has_its_controls(self):
        """A guard on the guard: if the pane markup were renamed, the check
        above would pass by finding nothing at all."""
        assert len(set(re.findall(r'onclick="(\w+)\(', slides_pane()))) >= 8


class TestTheShapeLibrary:
    @pytest.fixture(scope="class")
    @staticmethod
    def shapes():
        block = re.search(r"const SLIDE_SHAPES = \{(.*?)\n\};", SLIDES_JS, re.DOTALL)
        assert block, "SLIDE_SHAPES not found"
        found = dict(re.findall(r"\n    (\w+):\s*\{ label: '([^']+)'", block.group(1)))
        assert found, "no shapes parsed"
        return found, block.group(1)

    def test_there_are_enough_of_them(self, shapes):
        """Eleven was a box and an arrow. This is not a target for its own
        sake — it is the set a diagram needs before you stop reaching past it."""
        assert len(shapes[0]) >= 30

    @pytest.mark.parametrize("key", [
        "cylinder", "step", "corner",          # the diagram staples
        "arrowleft", "arrowup", "arrowdown", "arrowlr", "arrowud",
        "speech", "speechleft", "banner",      # a remark on a slide
    ])
    def test_the_ones_that_were_missing_are_there(self, key, shapes):
        assert key in shapes[0]

    def test_every_shape_is_in_a_declared_group(self, shapes):
        """A shape whose group is not in the list renders nowhere: the menu is
        built by walking the groups, so it would exist and be unreachable."""
        groups = re.search(r"const SLIDE_SHAPE_GROUPS = \[(.*?)\];", SLIDES_JS).group(1)
        declared = set(re.findall(r"'([^']+)'", groups))
        body = shapes[1]
        for key in shapes[0]:
            entry = re.search(rf"\n    {key}:\s*\{{(.*?)\}},", body, re.DOTALL)
            assert entry, key
            group = re.search(r"group: '([^']+)'", entry.group(1))
            assert group, f"{key} has no group"
            assert group.group(1) in declared, f"{key} is in {group.group(1)}"

    def test_every_shape_can_be_drawn(self, shapes):
        """`clip`, a radius, or the one that is a line. A shape with none of
        those is an indistinguishable rectangle."""
        body = shapes[1]
        for key in shapes[0]:
            entry = re.search(rf"\n    {key}:\s*\{{(.*?)\}},", body, re.DOTALL).group(1)
            drawable = ("clip: 'polygon" in entry or "clip: 'ellipse" in entry
                        or "radius:" in entry or key in ("rect", "line"))
            assert drawable, f"{key} draws as a plain rectangle"

    def test_the_polygons_are_closed_percentages(self, shapes):
        """A clip-path in px rather than % does not scale with the box, so the
        shape is right at one size and clipped at every other."""
        for clip in re.findall(r"clip: 'polygon\(([^']+)\)'", shapes[1]):
            assert "px" not in clip
            assert clip.count("%") >= 6


class TestTheMenuIsReadable:
    def test_it_is_grouped(self):
        """Thirty-four in one flat grid is a worse eleven."""
        assert "function shapeMenuHtml" in SLIDES_JS
        assert "shape-group" in SLIDES_JS
        assert ".shape-grid {" in CSS

    def test_the_names_moved_to_the_tooltip(self):
        """At eleven shapes the label under each swatch helped; at thirty-four
        it was most of the menu. Still reachable by pointer and by screen
        reader — it is the title and the aria-label."""
        assert "shape-name" not in SLIDES_JS
        assert "aria-label=" in SLIDES_JS

    def test_the_menu_cannot_run_off_the_screen(self):
        """A popup taller than the window is a popup whose last group does not
        exist."""
        rule = re.search(r"\.slides-shape-pop \{(.*?)\}", CSS, re.DOTALL).group(1)
        assert "max-height" in rule
        assert "overflow-y: auto" in rule

    def test_rounded_and_pill_are_not_drawn_as_the_ellipse(self):
        """The swatch is 22px and the shape is 200. A radius that reads as
        "slightly rounded" on the slide is a circle at this size, so `rounded`,
        `pill` and `ellipse` all drew as the same dot."""
        assert "swatch: '5px'" in SLIDES_JS
        assert 's.swatch !== undefined' in SLIDES_JS
        assert '.shape-swatch[data-shape="pill"]' in CSS
