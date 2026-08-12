"""The stylesheet had 25 font sizes and 15 radii, and none of it was decided.

That is what happens when every new panel picks a number that looks right next
to the last one: 11px, 11.5px and 12px end up inside a single card, all meaning
"small label". Three sizes a hair apart are not a hierarchy — they are noise
that makes an app feel unresolved without anyone being able to point at why.

These tests guard the property, not the numbers: sizes and radii come from a
named scale, and the scale stays small. A new panel that hard-codes 11.5px
fails here, which is the only way a scale survives contact with the next
feature.
"""
import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parents[1] / "carrot" / "web" / "css" / "style.css"


@pytest.fixture(scope="module")
def css():
    return CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rules(css):
    """The stylesheet with the :root scale definitions removed.

    The definitions are the one place raw pixel values belong; everything after
    them has to go through a variable.
    """
    return css.split("/* ===== Type =====", 1)[1]


class TestTheScalesExist:
    @pytest.mark.parametrize("name", [
        "--text-2xs", "--text-xs", "--text-sm", "--text-base", "--text-md",
        "--text-lg", "--text-xl", "--text-2xl", "--text-3xl",
    ])
    def test_type_steps(self, css, name):
        assert f"{name}:" in css

    @pytest.mark.parametrize("name", ["--r-xs", "--r-sm", "--r-lg", "--r-xl", "--r-pill"])
    def test_radius_steps(self, css, name):
        assert f"{name}:" in css

    @pytest.mark.parametrize("name", [f"--s-{n}" for n in range(1, 10)])
    def test_spacing_steps(self, css, name):
        assert f"{name}:" in css


class TestNothingHardCodesASize:
    def test_no_raw_font_size_in_pixels(self, rules):
        # `em` and `0` are fine: one is relative on purpose (inline code at
        # 0.88em of its surroundings), the other is a hide trick, and neither
        # introduces a new step.
        offenders = re.findall(r"font-size: *[0-9]+\.?[0-9]*px", rules)
        assert offenders == [], f"off-scale font sizes: {sorted(set(offenders))}"

    def test_no_raw_radius_in_pixels(self, rules):
        # 50% circles are excluded by the pattern — a circle is not a step on a
        # radius scale, it is a different shape.
        offenders = re.findall(r"border-[a-z-]*radius: *[0-9]+\.?[0-9]*px(?=\s*[;}])", rules)
        assert offenders == [], f"off-scale radii: {sorted(set(offenders))}"


class TestTheScaleStaysSmall:
    """A scale that grows every time somebody needs a size is not a scale."""

    def test_type_has_at_most_nine_steps(self, css):
        assert len(re.findall(r"--text-[a-z0-9]+:", css)) <= 9

    def test_radius_has_at_most_five_steps(self, css):
        assert len(re.findall(r"--r-[a-z]+:", css)) <= 5

    def test_the_steps_are_far_enough_apart_to_mean_something(self, css):
        # Two radii 2px apart read as two attempts at one object rather than as
        # two kinds of object.
        found = dict(re.findall(r"--r-(xs|sm|lg|xl): *([0-9]+)px", css))
        steps = sorted(int(v) for v in found.values())
        assert all(b - a >= 4 for a, b in zip(steps, steps[1:])), steps

    def test_type_steps_widen_as_they_grow(self, css):
        # A 1px difference is invisible at 30px and obvious at 11px, so the
        # gaps have to open up or the top of the scale is wasted steps.
        found = re.findall(r"--text-[a-z0-9]+: *([0-9]+)px", css)
        steps = [int(v) for v in found]
        gaps = [b - a for a, b in zip(steps, steps[1:])]
        assert steps == sorted(steps), steps
        assert gaps == sorted(gaps), gaps


class TestTheChoicesThatWereDeliberate:
    def test_the_reading_size_did_not_shrink(self, css):
        # Chat body is the one piece of text in the app people read at length,
        # and it was raised to 15px on purpose. A tidy-up that quietly took it
        # back to 13 would undo the fix that motivated it.
        #
        # A floor rather than an exact value: it went to 16px when the app
        # moved to a single typeface with a lower x-height, which is the same
        # fix continuing rather than a reversal of it. What must not happen is
        # it going *down*, and that is what this now says.
        import re
        size = int(re.search(r"--text-md: *(\d+)px", css).group(1))
        assert size >= 15, size
        body = css[css.index(".message .content {"):]
        assert "font-size: var(--text-md)" in body[:body.index("}")]

    def test_cards_keep_the_radius_they_already_had(self, css):
        # --radius has always been 14px and every card uses it. The most
        # visible surface in the app does not move to tidy a scale.
        assert "--radius: 14px" in css
        assert "--r-lg: 14px" in css
