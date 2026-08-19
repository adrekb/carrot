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
    # `[^{]*` rather than `\s*`: the light block carries a second selector
    # (`.paper`, for a white document inside a dark app), and pinning this to
    # the exact selector text made it fail on a change that did not touch a
    # single token.
    block = re.search(r':root\[data-theme="light"\][^{]*\{(.*?)\n\}', CSS, re.S)
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


def test_the_blank_chat_reaches_the_bottom_of_the_window():
    """The composer is `position: fixed`, so the view reserves space at its
    foot to stop it sitting on the terminal. With nothing said yet the composer
    moves to the middle of the page — and the reservation stayed, holding a
    place nothing was going to stand in, so the panel stopped 172px short and
    the empty state sat above a band of nothing.

    The reservation still has to come back the moment there are messages, which
    is why this is scoped to `.chat-blank` rather than removed.
    """
    assert "body.chat-blank #view-workspace" in CSS
    reserve = CSS[CSS.index("#view-workspace {"):]
    reserve = reserve[:reserve.index("}")]
    assert "--composer-h" in reserve, (
        "the normal reservation must still track the composer's real height")


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
        light = CSS[CSS.rindex(':root[data-theme="light"]'):]
        light = light[light.index("{"):light.index("\n}")]
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
        light = CSS[CSS.rindex(':root[data-theme="light"]'):]
        light = light[light.index("{"):light.index("\n}")]
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


# ===== Black =====
#
# The ground is #000000. Not a very dark grey that photographs as black —
# actual black, which on an OLED panel is the pixel switched off.
#
# It replaced a warm charcoal (#16150f, cream #f2ece0) that was a considered
# thing, and two of that theme's ideas had to survive the change: surfaces
# separate by luminance rather than by a line, and the accent stays warm.

def _lin(value):
    value /= 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(hex_value):
    hex_value = hex_value.lstrip("#")
    r, g, b = (int(hex_value[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _ratio(a, b):
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


_BLOCK = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _dark_token(name):
    """The winning value for the default dark root.

    Not the first declaration and not the last one in the file: style.css
    declares these in several `:root` blocks, and the light theme — which is
    an extra selector on `:root[data-theme="light"]` — comes after the dark
    one. Reading top-down gives a palette the app has never drawn; reading
    bottom-up gives the light theme.
    """
    stripped = _COMMENT.sub("", CSS)
    value = None
    for selectors, body in _BLOCK.findall(stripped):
        parts = [p.strip() for p in selectors.split(",")]
        if not any(p == ":root" for p in parts):
            continue
        for found_name, found_value in re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", body):
            if found_name == name:
                value = found_value.strip()
    assert value is not None, f"{name} is not defined on a bare :root"
    return value


class TestTheDarkThemeIsActuallyBlack:
    def test_the_ground_is_black(self):
        assert _dark_token("--bg") == "#000000"

    def test_the_nav_is_black_too(self):
        """A black page with a grey rail down the side is a grey app with a
        black hole in it."""
        assert _dark_token("--surface-nav") == "#000000"

    @pytest.mark.parametrize("token", ["--bg", "--bg2", "--card", "--card2", "--card3"])
    def test_the_ground_ramp_is_neutral(self, token):
        """The old ramp was warm on purpose. On a neutral black a warm tint
        reads as a brown haze rather than as a surface, so the steps are grey
        — the warmth moved entirely into the accent."""
        value = _dark_token(token).lstrip("#")
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
        assert max(r, g, b) - min(r, g, b) <= 6, f"{token} is tinted: {value}"

    def test_surfaces_still_separate_by_luminance(self):
        """The charcoal theme's own steps were 1.05, 1.07, 1.12, 1.14. Starting
        from black must not mean flattening everything into it."""
        ramp = [_dark_token(t) for t in ("--bg", "--bg2", "--card", "--card2", "--card3")]
        steps = [_ratio(a, b) for a, b in zip(ramp, ramp[1:])]
        assert all(step >= 1.05 for step in steps), steps

    @pytest.mark.parametrize("token,least", [("--text", 12.0), ("--muted", 6.0), ("--faint", 4.5)])
    def test_the_ink_clears_aa_on_a_card(self, token, least):
        """`--faint` is used at 11px, which is the worst case for it."""
        assert _ratio(_dark_token(token), _dark_token("--card")) >= least

    def test_the_ink_is_not_flat_white(self):
        """#fff on #000 is the one pairing that buzzes at the edges of type."""
        assert _dark_token("--text").lower() != "#ffffff"

    def test_the_accent_is_still_warm(self):
        """Orange on neutral black is the pairing the old warm ground was
        reaching for, and it reads better here than it did there."""
        assert _ratio("#ff7a2b", "#000000") > _ratio("#ff7a2b", "#16150f")

    def test_the_wash_is_turned_down(self):
        """I left this alone first, on a measurement: the strongest pool lifts
        the ground 1.21x on black against 1.30x on the charcoal, so it is
        quieter relative to what it sits on, not louder.

        That answered the wrong question. "Is it louder than it was" is not
        "does it belong on a theme whose whole point is #000000" — and on a
        black ground a coloured pool does not read as depth, it reads as the
        reason the app is not black. Not zero, because at this strength it is
        still a lit edge under the composer, which is where it was working."""
        strength = re.search(r"--wash-strength:\s*([0-9.]+);", CSS).group(1)
        assert 0 < float(strength) <= 0.5, strength
