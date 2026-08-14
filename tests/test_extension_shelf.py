"""An extension is not part of the app until you add it.

Every bundled pack used to be a card on one screen whether or not it had
anything to do with you, so Extensions read as a settings panel with a switch
per pack rather than as somewhere you go to get something. What is on the shelf
and what is in your app are different questions and they were one list.

The code still ships in the build. Fetching executable code from the internet
at runtime is the single thing a local-first assistant should not do — it would
put an arbitrary-code channel into an app whose whole promise is that nothing
leaves the machine unless you say so. "Download" here means that nothing an
extension contributes — no tool, no skill, no tab — exists until Add is pressed.
"""
import pytest

from carrot import config, extensions


@pytest.fixture(autouse=True)
def _clean_install(isolated_db):
    config.set_config("installed_extensions", [])
    config.set_config("enabled_extensions", [])


class TestTheShelf:
    def test_the_catalog_lists_everything_with_whether_it_is_added(self):
        catalog = {p["id"]: p for p in extensions.catalog()}
        assert "latexnote" in catalog
        assert catalog["latexnote"]["installed"] is False

    def test_your_list_starts_empty(self):
        assert extensions.list_packs() == []

    def test_adding_one_moves_it_across(self):
        extensions.install("latexnote")
        assert [p["id"] for p in extensions.list_packs()] == ["latexnote"]
        catalog = {p["id"]: p for p in extensions.catalog()}
        assert catalog["latexnote"]["installed"] is True

    def test_it_arrives_switched_off(self):
        """Adding is not enabling. Something that starts doing things the
        moment it is added is a thing people are wary of adding."""
        extensions.install("latexnote")
        assert extensions.is_enabled("latexnote") is False


class TestWhatIsNotAddedCannotAct:
    def test_a_pack_that_is_not_added_cannot_be_switched_on(self):
        with pytest.raises(ValueError):
            extensions.set_enabled("latexnote", True)

    def test_its_tools_are_not_offered(self):
        names = [t["function"]["name"] for t in extensions.ollama_tools()]
        assert not any("latexnote" in n for n in names)

    def test_its_tab_is_not_in_the_nav(self):
        assert "latex" not in extensions.pack_tabs()["enabled"]

    def test_all_three_arrive_together_once_it_is_added_and_switched_on(self):
        extensions.install("latexnote")
        extensions.set_enabled("latexnote", True)
        names = [t["function"]["name"] for t in extensions.ollama_tools()]
        assert any("note_outline" in n for n in names)
        assert "latex" in extensions.pack_tabs()["enabled"]


class TestRemoving:
    def test_removing_switches_it_off_on_the_way_out(self):
        """An extension that is not installed and still working is the worst
        of both readings."""
        extensions.install("latexnote")
        extensions.set_enabled("latexnote", True)
        extensions.uninstall("latexnote")
        assert extensions.is_enabled("latexnote") is False
        assert extensions.list_packs() == []
        assert "latex" not in extensions.pack_tabs()["enabled"]

    def test_a_stale_enabled_id_cannot_bring_it_back(self):
        """Removing leaves the id in the enabled list for anyone who had it on.
        Installation is checked first so that record cannot resurrect it."""
        config.set_config("enabled_extensions", ["latexnote"])
        config.set_config("installed_extensions", [])
        assert extensions.is_enabled("latexnote") is False


class TestTheMigration:
    def test_a_pack_you_already_had_switched_on_counts_as_added(self):
        """Anything already on was obviously installed. Migrating it to "not
        installed" would take a working pack away from somebody who turned it
        on deliberately, which is a worse first impression than an empty
        shelf."""
        config.set_config("installed_extensions", None)
        config.set_config("enabled_extensions", ["ambient"])
        assert extensions.is_installed("ambient") is True
        assert extensions.is_installed("academia") is False


class TestTheApi:
    def test_add_and_remove_over_http(self, client):
        catalog = client.get("/api/extensions/catalog").json()["extensions"]
        assert any(p["id"] == "latexnote" and not p["installed"] for p in catalog)

        assert client.post("/api/extensions/latexnote/install").status_code == 200
        mine = client.get("/api/extensions").json()["extensions"]
        assert [p["id"] for p in mine] == ["latexnote"]

        assert client.delete("/api/extensions/latexnote/install").status_code == 200
        assert client.get("/api/extensions").json()["extensions"] == []

    def test_adding_something_that_does_not_exist_is_a_404(self, client):
        assert client.post("/api/extensions/nope/install").status_code == 404


class TestTheLatexPack:
    def test_it_reads_both_heading_notations(self):
        """A document that mixes them is the normal case here: prose in
        markdown, structure in LaTeX, or the reverse depending on where the
        text came from."""
        from carrot.packs import latexnote

        found = latexnote.outline("# Intro\n\\section{Method}\n## Detail")
        assert [h["title"] for h in found] == ["Intro", "Method", "Detail"]
        assert [h["level"] for h in found] == [1, 1, 2]

    def test_the_statistics_count_the_mathematics(self):
        """A word count says nothing about a paper that is forty displayed
        equations, which is the document this pack is for."""
        from carrot.packs import latexnote

        stats = latexnote.statistics("Prose $a+b$ and\n$$E=mc^2$$\nmore prose.")
        assert stats["display_math"] == 1
        assert stats["inline_math"] == 1
        assert stats["words"] == 4

    def test_maths_is_not_counted_as_prose(self):
        from carrot.packs import latexnote

        assert latexnote.statistics("$$\\int_0^1 x^2 dx$$")["words"] == 0

    def test_it_does_not_ship_a_second_latex_toolchain(self):
        """Academia already compiles through a real engine and says so when
        one is missing. Two answers to "can I compile" means one of them is
        wrong."""
        from carrot.packs import latexnote

        assert not any("compile" in name for name in latexnote.TOOLS)
        assert latexnote.PACK.capabilities == []

    def test_the_editing_skill_says_to_return_only_the_fragment(self):
        """What comes back is substituted directly for the selection, so a
        sentence of commentary becomes a sentence of commentary in the middle
        of the document."""
        from carrot.packs import latexnote

        instructions = latexnote.SKILLS[0]["instructions"]
        assert "nothing else" in instructions
        assert "\\label" in instructions
