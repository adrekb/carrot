"""The select popup is drawn by the app, not by the OS.

Closed, a `<select>` here looks like everything else: the stylesheet gives it
the app's border, radius, font and colours. Opened, none of that applied — the
popup belongs to the operating system, not to the document, so on Windows it
came up as a white list in the system font over a dark app, sized to its
longest option, and the model picker (every model on every configured
provider) ran off the bottom of the window with the model in use somewhere
below the fold.

js/dropdown.js replaces the popup and keeps the `<select>`. That choice is
what these tests are mostly about: every call site still reads `.value`, every
`onchange=` in the markup still fires, and a `<select>` built by JS in a panel
that has never heard of this file is upgraded the first time it is clicked.
"""
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "carrot" / "web"
DROPDOWN = (WEB / "js" / "dropdown.js").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "css" / "style.css").read_text(encoding="utf-8")


def test_the_replacement_is_loaded_by_the_page():
    assert '/js/dropdown.js' in INDEX


@pytest.mark.parametrize("cls", [".dd-menu", ".dd-item", ".dd-group", ".dd-mark", ".dd-text"])
def test_every_class_it_builds_is_styled(cls):
    """Unstyled, the menu is an unpositioned block of text at the end of the page."""
    assert cls in CSS, f"{cls} is built by dropdown.js and never styled"


def test_the_menu_sits_above_every_other_layer():
    """A menu opened from a settings modal must not render behind it."""
    others = [int(z) for z in re.findall(r"z-index: *([0-9]+)", CSS)]
    menu = CSS[CSS.index(".dd-menu {"):]
    top = int(re.search(r"z-index: *([0-9]+)", menu).group(1))
    assert top == max(others)


def test_option_text_is_never_written_as_html():
    """Option labels include model ids straight from a provider's API."""
    code = "\n".join(line.split("//", 1)[0] for line in DROPDOWN.splitlines())
    assert "innerHTML" not in code
    assert "textContent = option.textContent" in code


def test_choosing_fires_both_events_the_app_listens_for():
    """`onchange=` in the markup and addEventListener('input') in the settings
    panels are both in use, and a native popup fires both. Anything less would
    look like the picker silently not saving."""
    assert "new Event('input'" in DROPDOWN
    assert "new Event('change'" in DROPDOWN
    assert "bubbles: true" in DROPDOWN


def test_a_list_box_is_left_alone():
    """A `multiple` or sized select is drawn inline and has no popup to
    replace; intercepting its clicks would break its only interaction."""
    assert "el.multiple" in DROPDOWN
    assert "el.size > 1" in DROPDOWN


def test_the_menu_is_height_capped_and_kept_on_screen():
    """The failure that started this: a hundred options past the window edge."""
    assert "maxHeight" in DROPDOWN
    assert "window.innerHeight" in DROPDOWN


def test_type_ahead_survives_the_replacement():
    """Native popups have it, and the list this exists for is a hundred model
    ids — losing it would make the replacement worse for the menu that needs
    it most."""
    assert "startsWith" in DROPDOWN


def test_no_call_site_had_to_change():
    """The whole design. If a panel has to adopt a component, the twenty-odd
    menus nobody remembers to convert keep looking like Windows."""
    assert INDEX.count("<select") >= 20
    # Upgraded by listening at the document, not by wrapping each element.
    assert "document.addEventListener('mousedown'" in DROPDOWN
