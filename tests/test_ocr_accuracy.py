"""Does the OCR actually read?

Every other ambient test mocks OCR out or asserts the refusal path, so until
now nothing measured the one input everything downstream is built on. That
matters more here than usual because the failure is silent and it compounds:
bad OCR produces a plausible row of text that goes into FTS *and* into an
embedding, and recall then returns confident nonsense with no score anywhere
marking it as doubtful.

The fixtures are rendered rather than screenshotted, so the expected text is
the input rather than somebody's transcription of it — see
`tests/fixtures/ocr/make_fixtures.py` for why, and for what that does and does
not prove. Short version: this is a floor, not a benchmark. Rendered text is
cleaner than a real window. An engine failing these is broken; an engine
passing them is not thereby good.

The thresholds are set from what the engine on this machine actually scores,
with headroom, so they catch a regression rather than encoding an aspiration.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

import pytest

from carrot import ambient_capture

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ocr"

pytestmark = pytest.mark.skipif(
    ambient_capture.available_ocr() is None,
    reason="no OCR engine on this machine — nothing to measure",
)


def _cases():
    return json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))


def _words(text: str):
    """Tokens as a searcher would meet them.

    Punctuation is kept inside a token rather than split on, because that is
    what makes `limit=6` and `agent_may_search()` findable. Splitting it away
    would score an engine that drops every bracket as perfect.
    """
    return re.findall(r"[\w'./:=(){}\[\],*-]+", (text or "").lower())


def _accuracy(expected: str, got: str) -> float:
    return difflib.SequenceMatcher(None, _words(expected), _words(got)).ratio()


def _read(case) -> str:
    from PIL import Image

    text, _engine = ambient_capture.ocr_image(Image.open(FIXTURES / case["file"]))
    return text


# Measured on Windows.Media.Ocr: prose, dark mode and UI labels all read at
# 1.000; small text at 0.930; code at 0.531. The floors sit below each with
# room for a language-pack difference, and the code floor is deliberately low
# because the engine genuinely is poor there — see TestWhatItCannotRead, which
# records that rather than letting a lax number hide it.
FLOORS = {
    "prose_light": 0.95,
    "prose_dark": 0.95,
    "ui_labels": 0.95,
    "small_text": 0.85,
    "code_punctuation": 0.45,
}


class TestItReads:
    @pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
    def test_each_fixture_clears_its_floor(self, case):
        score = _accuracy(case["text"], _read(case))
        assert score >= FLOORS[case["name"]], (
            f"{case['name']} read at {score:.3f}, floor {FLOORS[case['name']]}")

    def test_dark_mode_reads_as_well_as_light(self):
        """An engine that reads one polarity and not the other indexes half of
        somebody's day and says nothing about the missing half."""
        cases = {c["name"]: c for c in _cases()}
        light = _accuracy(cases["prose_light"]["text"], _read(cases["prose_light"]))
        dark = _accuracy(cases["prose_dark"]["text"], _read(cases["prose_dark"]))
        assert dark >= light - 0.15, f"dark {dark:.3f} vs light {light:.3f}"

    def test_the_mean_across_everything(self):
        cases = _cases()
        mean = sum(_accuracy(c["text"], _read(c)) for c in cases) / len(cases)
        assert mean >= 0.80, f"mean accuracy {mean:.3f}"

    def test_something_was_actually_read(self):
        """The failure this whole file exists for: an engine that returns ""
        scores zero everywhere, and a suite that only checked ratios above a
        floor would report five failures without naming the cause."""
        for case in _cases():
            assert _read(case).strip(), f"{case['name']} produced no text at all"


class TestWhatItCannotRead:
    """Known limitations, recorded rather than hidden in a lax threshold.

    A gap nobody wrote down gets rediscovered as a bug report. These are the
    two the fixtures found on the first run.
    """

    def test_underscores_in_identifiers_are_lost(self):
        """Windows OCR reads `search_for_agent` as `search for agent`.

        So code on screen is indexed under names that are not the names. Asking
        `search_screen` about "the search_for_agent error" will not find the
        frame that had it on screen, and nothing anywhere says why.

        Asserted as it currently behaves so the day it improves, this fails and
        somebody deletes it — which is the point of writing it down.
        """
        cases = {c["name"]: c for c in _cases()}
        got = _read(cases["code_punctuation"])
        assert "search_for_agent" not in got
        assert "search for agent" in got

    def test_digit_group_separators_pick_up_a_space(self):
        """`1,911` comes back as `1 ,911`, so a frame is not findable by a
        number the way it was written on screen."""
        cases = {c["name"]: c for c in _cases()}
        got = _read(cases["small_text"])
        assert "1,911" not in got


class TestTheFixturesThemselves:
    def test_every_fixture_file_is_present(self):
        for case in _cases():
            assert (FIXTURES / case["file"]).exists(), case["file"]

    def test_the_expected_text_is_not_empty(self):
        for case in _cases():
            assert case["text"].strip(), f"{case['name']} has no ground truth"

    def test_every_case_has_a_floor(self):
        """A fixture added without one would be generated, read, and silently
        never asserted against."""
        assert {c["name"] for c in _cases()} == set(FLOORS)
