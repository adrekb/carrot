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


def test_the_stylesheet_has_no_control_characters():
    """A regex rewrite once left `background\x01:` in 58 rules.

    The property is invalid, so every browser silently dropped it and the
    backgrounds simply did not apply — no error anywhere, and nothing to see
    unless you happened to look at the one rule you were editing.
    """
    bad = [i for i, ch in enumerate(CSS) if ord(ch) < 32 and ch not in "\n\r\t"]
    assert not bad, f"control characters in style.css at offsets {bad[:5]}"
