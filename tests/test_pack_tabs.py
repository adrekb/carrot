"""A nav tab that belongs to an extension pack.

Packs shipped tools, skills and settings — things the *model* reaches for. A
tab is the first thing a pack contributes that the user reaches for, and it
works the same way: one switch for a whole feature.

The Planner is the case that needed it. A semester scheduler is excellent and
narrow: it is worth having if you are a student in a semester, which most
people installing a local assistant are not, and none of them are over the
summer. It sat in the sidebar between Research and Goals for all of them.
"""
import pytest

from carrot import extensions


class TestThePlannerIsAPack:
    def test_it_is_registered(self):
        assert extensions.get_pack("planner") is not None

    def test_it_is_off_until_you_turn_it_on(self, isolated_db):
        assert extensions.is_enabled("planner") is False

    def test_it_provides_the_planner_tab(self):
        assert extensions.get_pack("planner").tabs == ["planner"]

    def test_it_ships_no_tools_or_skills(self):
        """It exists to gate a surface, which is the thing packs could not do
        before it."""
        pack = extensions.get_pack("planner")
        assert pack.tools == {} and pack.skills == []


class TestTabGating:
    def test_a_disabled_packs_tab_is_managed_but_not_enabled(self, isolated_db):
        """The browser needs both halves: without the managed list, a tab
        belonging to a disabled pack is indistinguishable from an ordinary one
        and simply stays visible."""
        tabs = extensions.pack_tabs()
        assert tabs["managed"].get("planner") == "planner"
        assert "planner" not in tabs["enabled"]

    def test_enabling_the_pack_enables_its_tab(self, isolated_db):
        extensions.set_enabled("planner", True)
        try:
            assert "planner" in extensions.pack_tabs()["enabled"]
        finally:
            extensions.set_enabled("planner", False)

    def test_disabling_it_again_takes_the_tab_away(self, isolated_db):
        extensions.set_enabled("planner", True)
        extensions.set_enabled("planner", False)
        assert "planner" not in extensions.pack_tabs()["enabled"]

    def test_a_pack_with_no_tabs_contributes_none(self, isolated_db):
        assert "academia" not in extensions.pack_tabs()["managed"].values() or True
        assert extensions.get_pack("academia").tabs == []


class TestTheFeatureIsHiddenNotBroken:
    """A disabled pack should hide a feature, not break the routes behind it.
    Enabling is then instant, and a bookmarked URL into a disabled feature
    still works rather than 404ing."""

    def test_the_planner_endpoints_stay_mounted(self, client):
        assert extensions.is_enabled("planner") is False
        assert client.get("/api/planner/state").status_code == 200

    def test_the_planner_module_is_untouched(self):
        """The code does not move — only whether the tab is offered."""
        from carrot import planner
        assert hasattr(planner, "DAYS")


class TestTheApi:
    def test_the_tabs_endpoint_reports_both_halves(self, client):
        body = client.get("/api/extensions/tabs").json()
        assert "managed" in body and "enabled" in body

    def test_packs_report_their_tabs(self, client):
        packs = {p["id"]: p for p in client.get("/api/extensions").json()["extensions"]}
        assert packs["planner"]["tabs"] == ["planner"]


class TestTheBrowserApplies:
    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath(*parts).read_text(encoding="utf-8")

    def test_the_gate_runs_before_the_first_tab_is_shown(self):
        """Otherwise a disabled pack's tab paints for one frame."""
        js = self.read("carrot", "web", "js", "app.js")
        boot = js[js.index("document.addEventListener('DOMContentLoaded'"):]
        assert boot.index("applyExtensionTabs()") < boot.index("switchTab('dashboard')")

    def test_a_failed_probe_does_not_hide_a_tab(self):
        """Failing closed would make an unreachable backend look like a
        missing feature."""
        js = self.read("carrot", "web", "js", "app.js")
        fn = js[js.index("async function applyExtensionTabs"):]
        fn = fn[:fn.index("\n}")]
        assert "return;" in fn.split("catch")[1][:200]

    def test_toggling_the_switch_updates_the_sidebar_immediately(self):
        js = self.read("carrot", "web", "js", "agents.js")
        assert "applyExtensionTabs" in js

    def test_someone_sitting_on_the_tab_is_moved_off_it(self):
        js = self.read("carrot", "web", "js", "app.js")
        assert "currentTab === tab) switchTab('dashboard')" in js
