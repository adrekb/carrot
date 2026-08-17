"""Appearance: light/dark/auto themes and accent palettes.

These guard the two ways the feature breaks silently. The theme is chosen by
the browser before it can call the API, so the CSS has to be complete and the
bootstrap script has to run before the first paint — neither failure raises
an error anywhere, they just look wrong.
"""
import re
from pathlib import Path

import pytest

from carrot import config

WEB = Path(__file__).resolve().parent.parent / "carrot" / "web"
CSS = (WEB / "css" / "style.css").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
THEME_JS = (WEB / "js" / "theme.js").read_text(encoding="utf-8")

ACCENTS = ["carrot", "ember", "amber", "orchid", "teal", "indigo"]


def test_appearance_defaults_exist():
    assert config.DEFAULTS["ui_theme"] == "auto"
    assert config.DEFAULTS["ui_accent"] == "carrot"


def test_appearance_keys_are_not_secrets():
    """They are set through the generic config endpoint, which refuses secrets."""
    assert "ui_theme" not in config.SECRET_KEYS
    assert "ui_accent" not in config.SECRET_KEYS


def test_light_theme_overrides_every_ground_token():
    """A token the light block forgets keeps its dark value — e.g. dark text
    on a dark card over a white page, which is invisible rather than merely
    ugly. Check the ones that carry contrast."""
    block = re.search(r':root\[data-theme="light"\]\s*\{(.*?)\n\}', CSS, re.S)
    assert block, "no light theme block"
    body = block.group(1)
    for token in ("--bg", "--bg2", "--card", "--card2", "--card3",
                  "--text", "--muted", "--faint", "--border", "--border-hi",
                  "--opt-bg", "--opt-text", "--scheme"):
        assert f"{token}:" in body, f"light theme does not override {token}"


@pytest.mark.parametrize("accent", ACCENTS)
def test_every_accent_defines_the_full_ramp(accent):
    """The picker offers these by id; an id with no CSS block silently falls
    through to the previous accent, so the swatch appears to do nothing."""
    if accent == "carrot":
        block = re.search(r':root,\s*:root\[data-accent="carrot"\]\s*\{(.*?)\n\}', CSS, re.S)
    else:
        block = re.search(r':root\[data-accent="%s"\]\s*\{(.*?)\n\}' % accent, CSS, re.S)
    assert block, f"no CSS block for accent {accent}"
    body = block.group(1)
    for token in ("--accent", "--accent-hi", "--accent-dim",
                  "--accent-soft", "--accent-line"):
        assert f"{token}:" in body, f"accent {accent} is missing {token}"


def test_accent_ids_match_between_css_and_picker():
    listed = re.findall(r"id:\s*'([a-z]+)'", THEME_JS)
    assert listed == ACCENTS


def test_wash_hues_are_accent_scoped():
    """The ambient glow is what actually carries the theme; if an accent does
    not set the wash triplet the window keeps the previous accent's light."""
    for accent in ACCENTS:
        pattern = (r':root,\s*:root\[data-accent="carrot"\]' if accent == "carrot"
                   else r':root\[data-accent="%s"\]' % accent)
        block = re.search(pattern + r'\s*\{(.*?)\n\}', CSS, re.S)
        body = block.group(1)
        for token in ("--wash-a", "--wash-b", "--wash-c"):
            assert f"{token}:" in body, f"accent {accent} is missing {token}"


def test_theme_script_loads_before_the_body():
    """Loaded late, the browser paints one frame of the default dark theme —
    a black flash on every launch for a light-mode user."""
    head = INDEX.split("</head>", 1)[0]
    assert "/js/theme.js" in head, "theme.js must load from <head>"
    tag = re.search(r'<script[^>]*src="/js/theme\.js"[^>]*>', head).group(0)
    assert "defer" not in tag and "async" not in tag, \
        "theme.js must run synchronously or the theme lands after first paint"


def test_settings_exposes_the_appearance_picker():
    assert 'id="theme-modes"' in INDEX
    assert 'id="theme-accents"' in INDEX


def test_auto_is_never_written_to_the_dom():
    """The stylesheet only knows dark and light. 'auto' has to be resolved in
    JS, so no rule should ever try to match it."""
    assert '[data-theme="auto"]' not in CSS


def test_no_window_prompt_left_in_the_web_ui():
    """Electron disables window.prompt(): it returns null without showing
    anything, so every caller became a button that does nothing."""
    offenders = []
    for path in sorted((WEB / "js").glob("*.js")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("//", 1)[0]
            if re.search(r"(?<![.\w])prompt\s*\(", code) and "inlineTextPrompt" not in code:
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, f"window.prompt() is disabled in Electron: {offenders}"


def test_inline_prompt_has_styling():
    """The replacement modal is built from these class names; unstyled it is
    an unpositioned block at the bottom of the page."""
    for cls in (".path-prompt", ".path-prompt-card", ".path-prompt-title"):
        assert cls in CSS, f"{cls} is used by inlineTextPrompt() but never styled"


class TestWhereTheAnswerComesFrom:
    """A mark, not a sentence.

    The empty state said "everything runs on your machine" unconditionally,
    which was false with a hosted model selected — a privacy claim that is
    wrong in the one place people read it is worse than none. Made accurate, it
    became prose: "Answers come from ministral-14b-latest over the internet", a
    model id and a caveat directly under a five-word heading. Local or not is a
    state, and a state is a glyph.
    """

    JS = (WEB / "js" / "app.js").read_text(encoding="utf-8")

    def _block(self):
        start = self.JS.index("function renderEmptyStateLine")
        return self.JS[start:self.JS.index("\n}", start)]

    def test_both_glyphs_exist_to_point_at(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        for icon in ('id="i-cloud"', 'id="i-computer"', 'id="i-info"'):
            assert icon in html, f"{icon} is used by the empty state but never defined"

    def test_it_picks_the_glyph_off_the_state(self):
        block = self._block()
        assert "i-computer" in block and "i-cloud" in block

    def test_the_sentence_is_still_reachable(self):
        """Hidden, not deleted. Which model and why is a fair question; it just
        should not be the first thing on the screen."""
        block = self._block()
        assert "title=" in block, "the glyph needs to say what it means on hover"
        assert "where-why" in block, "and there should be something to press"

    def test_a_screen_reader_still_gets_words(self):
        """A glyph with no text is a decoration to anything that cannot see
        it, and this one is carrying a privacy claim."""
        assert "sr-only" in self._block()

    def test_the_marks_are_styled(self):
        for cls in (".where-mark", ".where-why", ".where-said"):
            assert cls in CSS, f"{cls} is built by renderEmptyStateLine but never styled"


class TestTextOnColour:
    """A sweep of 4682 rendered text nodes across six accents and both themes
    found eighteen selectors under the contrast they needed. Almost all were
    one of three mistakes, and these are the guards for those three.

    The measuring itself cannot live here — it needs a browser — so what is
    pinned is the shape of the fixes, which is what regresses.
    """

    def test_the_palette_carries_ink_for_each_kind_of_surface(self):
        """Three, because they are three different backgrounds and one answer
        cannot serve them: `--on-accent` for the fill, `--on-accent-bright` for
        the bright end of the ramp, `--accent-ink` for the accent as words on
        the page. Orchid needs white on one and near-black on another."""
        for token in ("--on-accent:", "--on-accent-bright:", "--accent-ink:"):
            assert token in CSS, f"{token} is missing from the palette"

    def test_status_colours_are_themed(self):
        """One value for both themes meant the dark-theme green and yellow
        measured 1.87 and 1.64 on a light background — very nearly invisible."""
        light = CSS[CSS.rindex(':root[data-theme="light"] {'):]
        light = light[:light.index("\n}")]
        for token in ("--green:", "--yellow:", "--red:"):
            assert token in light, f"{token} has no light-theme value"

    def test_no_status_colour_is_hardcoded_past_the_palette(self):
        """The badges wrote `rgb(126, 200, 128)` inline, so a themed `--green`
        could never have reached them."""
        for literal in ("rgb(126, 200, 128)", "rgb(233, 176, 74)"):
            assert literal not in CSS, (
                f"{literal} is a status colour written past the tokens")

    def test_the_primary_button_does_not_write_white_on_the_bright_accent(self):
        """`--accent` is the end of the ramp meant to be noticed rather than
        read against: white on it measured 3.55 on orchid."""
        rule = CSS[CSS.rindex(".btn-primary {"):]
        rule = rule[:rule.index("}")]
        assert "#fff" not in rule
        assert "--on-accent" in rule

    def test_faint_is_calibrated_against_its_worst_background(self):
        """It was measured on `--card`, the lightest surface in the theme, and
        then used on the darker ones where the same colour lands at 4.33."""
        light = CSS[CSS.rindex(':root[data-theme="light"] {'):]
        light = light[:light.index("\n}")]
        assert "--card2" in light[:light.index("--faint:")], (
            "the --faint comment should name the surface it was measured against")


def test_the_small_print_on_a_chosen_option_is_not_left_grey():
    """`.ctx-hint` and `.ctx-tokens` set their own colour.

    So they win on specificity over whatever `.ctx-choice.on` switches the
    button to, and the hint stayed the grey chosen to sit quietly on a dark
    card — now on a saturated accent fill. Measured against its own background
    it ran 1.10–1.94 across the six accents; on teal, 1.10. The recommendation
    under the option you had picked was the least readable sentence on screen.

    The fix has to name the children, because setting the colour on the parent
    is exactly what did not work.
    """
    assert re.search(
        r"\.ctx-choice\.on\s+\.ctx-hint\s*,\s*\.ctx-choice\.on\s+\.ctx-tokens\s*\{[^}]*color:", CSS
    ), "the hint and tokens need their own colour when the card is selected"


def test_the_stylesheet_has_no_control_characters():
    """A regex rewrite once left `background\x01:` in 58 rules.

    The property is invalid, so every browser silently dropped it and the
    backgrounds simply did not apply — no error anywhere, and nothing to see
    unless you happened to look at the one rule you were editing.
    """
    bad = [i for i, ch in enumerate(CSS) if ord(ch) < 32 and ch not in "\n\r\t"]
    assert not bad, f"control characters in style.css at offsets {bad[:5]}"
