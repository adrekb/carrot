"""Settings in groups rather than one scroll.

Twenty-one cards on one page is a page you search rather than read: everything
is on it, so nothing is anywhere in particular, and the way to change your
model was to remember roughly how far down it lived.

The cards are tagged in the markup rather than moved. Reordering twenty-one
blocks of HTML to group them would be a diff nobody can review, and the tag is
the same fact in one attribute.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "js" / "context.js").read_text(encoding="utf-8")
CSS = (WEB / "css" / "style.css").read_text(encoding="utf-8")


def settings_view():
    """The settings section only.

    It is the last `<section id="view-…">` in the file, so slicing to "the next
    view" finds nothing — the boundary is its own closing tag.
    """
    start = INDEX.index('<section id="view-settings"')
    end = INDEX.index("\n  </section>", start)
    return INDEX[start:end]


def declared_tabs():
    block = re.search(r"SETTINGS_TABS = \[(.*?)\n\];", JS, re.DOTALL).group(1)
    return [t for t, _ in re.findall(r"\['([a-z]+)',\s*'([^']+)'\]", block)]


class TestEveryCardIsInAGroup:
    def test_none_is_left_untagged(self):
        """An untagged card is one that never shows, in a page where the only
        symptom is a setting you cannot find."""
        view = settings_view()
        total = view.count('<div class="settings-card"')
        tagged = view.count("data-settings-tab=")
        assert tagged == total, f"{total - tagged} cards have no group"

    def test_every_tag_is_a_declared_group(self):
        used = set(re.findall(r'data-settings-tab="([a-z]+)"', settings_view()))
        assert used <= set(declared_tabs()), sorted(used - set(declared_tabs()))

    def test_every_group_has_something_in_it(self):
        """A tab that shows an empty page is a tab that should not be there."""
        used = set(re.findall(r'data-settings-tab="([a-z]+)"', settings_view()))
        assert set(declared_tabs()) <= used, sorted(set(declared_tabs()) - used)

    @pytest.mark.parametrize("heading,group", [
        ("Providers", "models"),
        ("Task Routing", "models"),
        ("How much a local model holds in mind", "models"),
        ("Appearance", "general"),
        ("Add-ons", "tools"),
        ("Local webhooks", "tools"),
        ("Google Calendar", "connections"),
        ("GitHub Contributions", "connections"),
        ("What Carrot is allowed to see", "privacy"),
        ("Asking about what you saw", "privacy"),
    ])
    def test_a_card_is_where_you_would_look_for_it(self, heading, group):
        """The group is the thing you know before the setting's name — "it's a
        model thing" arrives before "it's called Task Routing"."""
        view = settings_view()
        at = view.index(f"<h3>{heading}")
        card = view.rindex('<div class="settings-card"', 0, at)
        assert f'data-settings-tab="{group}"' in view[card:card + 80]


class TestTheStrip:
    def test_it_exists_and_is_a_tablist(self):
        assert 'id="settings-tabs"' in settings_view()
        assert 'role="tablist"' in settings_view()

    def test_it_sits_above_the_cards(self):
        view = settings_view()
        assert view.index('id="settings-tabs"') < view.index("data-settings-tab=")

    def test_the_counts_come_from_the_page(self):
        """So a card added to a group is counted without anybody remembering to
        update a list here."""
        body = re.search(r"function renderSettingsTabs\(\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "querySelectorAll" in body

    def test_the_chosen_group_is_remembered(self):
        """Settings is a page you come back to for the same thing twice."""
        assert "localStorage.setItem('carrot-settings-tab'" in JS
        assert "localStorage.getItem('carrot-settings-tab')" in JS

    def test_an_unknown_stored_group_falls_back(self):
        """A group removed in a later version must not leave someone on a blank
        page with no way to tell why."""
        body = re.search(r"function restoreSettingsTab\(\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "SETTINGS_TABS.some" in body

    def test_switching_returns_to_the_top(self):
        """Switching group while scrolled down the last one lands you in the
        middle of the new one with no sign you have moved."""
        body = re.search(r"function setSettingsTab\(id\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "scrollTo" in body

    def test_the_selection_is_announced(self):
        body = re.search(r"function setSettingsTab\(id\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "aria-selected" in body

    def test_it_only_touches_settings_cards(self):
        """`.settings-card` is used in other views too — the memory page, the
        planner. Hiding those would empty three pages to tidy one."""
        body = re.search(r"function setSettingsTab\(id\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "[data-settings-tab]" in body

    def test_every_css_token_it_uses_is_defined(self):
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", CSS, re.MULTILINE))
        used = set()
        for selector in (".settings-tabs", ".settings-tab"):
            rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS).group(1)
            used |= set(re.findall(r"var\((--[a-z0-9-]+)", rule))
        assert used <= defined, sorted(used - defined)
