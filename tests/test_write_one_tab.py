"""Write is one tab, and a wikilink survives being edited.

Notes, LaTeX and Graph were three nav entries. Opening a paper from the
document list threw you into a tab that had no document list in it, and the
graph was a fourth destination for something that is only a way of reading the
list. They are all panes in Write now.
"""
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "carrot" / "web"


def read(*parts):
    return WEB.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index():
    return read("index.html")


@pytest.fixture(scope="module")
def features():
    return read("js", "features.js")


class TestTheNavHasOneWriteEntry:
    @pytest.mark.parametrize("tab", ["latex", "graph"])
    def test_the_folded_tabs_have_no_nav_entry(self, index, tab):
        assert f'data-tab="{tab}"' not in index

    @pytest.mark.parametrize("tab", ["latex", "graph"])
    def test_the_folded_tabs_have_no_view_section(self, index, tab):
        """A `<section id="view-x">` left behind is a page you can still reach
        by deep link and which no longer has anything in it."""
        assert f'<section id="view-{tab}"' not in index

    def test_write_is_still_reachable(self, index):
        assert 'data-tab="notes"' in index
        assert '<section id="view-notes"' in index

    def test_asking_for_a_folded_tab_lands_in_write(self):
        """A saved deep link or an older call site must not open a blank
        screen where a section used to be."""
        app = read("js", "app.js")
        folded = re.search(r"FOLDED_INTO_WRITE\s*=\s*\{([^}]*)\}", app)
        assert folded, "no redirect table for the tabs that became panes"
        assert "latex" in folded.group(1)
        assert "graph" in folded.group(1)


class TestEveryFormatIsAPane:
    @pytest.mark.parametrize("pane", ["latex-pane", "graph-pane", "canvas-pane", "slides-pane"])
    def test_the_pane_lives_inside_write(self, index, pane):
        start = index.index('<section id="view-notes"')
        end = index.index('<section id="view-', start + 1)
        assert f'id="{pane}"' in index[start:end]

    @pytest.mark.parametrize("mode", ["prose", "latex", "canvas", "slides", "graph", "start"])
    def test_the_mode_is_declared(self, features, mode):
        """One table decides what each mode shows. A format missing from it is
        a pane that never hides, which is how two editors end up on screen."""
        modes = re.search(r"WRITE_MODES\s*=\s*\{(.*?)\n\};", features, re.DOTALL)
        assert modes, "WRITE_MODES table not found"
        assert re.search(rf"\b{mode}:", modes.group(1))

    def test_opening_a_latex_document_does_not_leave_write(self):
        """`switchTab('latex')` is what made opening a paper lose the list."""
        code = "\n".join(line.split("//", 1)[0] for line in read("js", "latexnote.js").splitlines())
        assert "switchTab('latex')" not in code
        assert "showWriteMode('latex')" in code


class TestTheRail:
    """Three panels answering "where am I" in three different corners of the
    app are one column that does not move."""

    @pytest.mark.parametrize("section", ["rail-backlinks", "rail-outline", "rail-canvas"])
    def test_each_rail_section_exists(self, index, section):
        assert f'id="{section}"' in index

    def test_the_rail_is_one_element(self, index):
        assert index.count('id="doc-rail"') == 1

    @pytest.mark.parametrize("moved", ["latex-outline", "canvas-nav-list", "note-backlinks"])
    def test_what_moved_into_it_is_not_duplicated(self, index, moved):
        """Moving a panel by copying it leaves two elements with one id, and
        the one that gets updated is whichever the browser found first."""
        assert index.count(f'id="{moved}"') == 1


class TestAWikilinkSurvivesTheEditor:
    """Milkdown escapes `[` on the way out, so `[[Philosophers]]` came back as
    `\\[\\[Philosophers]]`. Nothing looked wrong — the escape renders
    invisibly — but the link stopped parsing, so opening a note and letting
    autosave run silently unlinked the whole document."""

    def test_the_editor_output_is_unescaped_before_it_is_saved(self, features):
        assert "function unescapeWikilinks" in features
        saved = re.search(r"function getEditorMarkdown\(\)\s*\{(.*?)\n\}", features, re.DOTALL)
        assert saved, "getEditorMarkdown not found"
        assert "unescapeWikilinks" in saved.group(1), \
            "the editor's markdown reaches the save path without being unescaped"

    def test_the_fix_undoes_the_editor_rather_than_the_author(self, features):
        """Deliberately client-side, and worth saying why.

        Normalising on the server would be easier to test and would catch every
        client at once — and would also rewrite somebody who typed `\\[\\[` on
        purpose to write *about* wikilinks without making one. This undoes a
        transformation the editor applied on its way out, which is the only
        place that distinction is still known.
        """
        body = re.search(r"function unescapeWikilinks[^\n]*\n(.*?)\n\}", features, re.DOTALL)
        assert body, "unescapeWikilinks not found"
        # It rebuilds a bare `[[...]]`, which is what the parser reads.
        assert "'[[' +" in body.group(1)

    def test_the_server_does_not_also_rewrite_bodies(self):
        """The other half of the same decision: notes.py stores what it is
        given. A second, disagreeing normaliser is how round trips start
        depending on which one ran last."""
        notes_py = (Path(__file__).resolve().parents[1] / "carrot" / "notes.py"
                    ).read_text(encoding="utf-8")
        assert "unescape" not in notes_py.lower()
        assert r"\[\[" not in notes_py


class TestTheDocumentBrowser:
    """No list down the side.

    A permanent sidebar of every document is a column of whichever note repeats
    most — and it is wrong for both things people do here: start something, or
    go back to one of the handful they were last in. Both are on the screen you
    land on, which is where the search and the filters are.
    """

    def test_there_is_no_document_sidebar(self, index):
        start = index.index('<section id="view-notes"')
        end = index.index('<section id="view-', start + 1)
        assert 'class="split-side"' not in index[start:end]
        assert 'id="notes-list"' not in index

    def test_the_browser_can_be_searched(self, index):
        assert 'id="notes-filter"' in index

    @pytest.mark.parametrize("control", ["write-filter-type", "write-filter-date",
                                         "write-filter-workspace"])
    def test_it_filters_by_kind_date_and_workspace(self, index, control):
        assert f'id="{control}"' in index

    def test_every_editor_offers_a_way_back(self, index):
        """With no sidebar, a document you have opened is a room with no door
        unless each toolbar has one — and it has to be in the same place in
        every format or it is five different doors."""
        start = index.index('<section id="view-notes"')
        end = index.index('<section id="view-', start + 1)
        # prose, latex, canvas, slides, graph
        assert index[start:end].count('class="icon-btn write-back"') == 5

    def test_filtering_is_not_capped_to_the_shelf(self, features):
        """Unfiltered this is a recents shelf and a dozen or so is the point.
        Filtered it is a search result, and truncating one silently would be a
        lie about how many documents match."""
        body = re.search(r"function renderWriteStartRecents\(\)\s*\{(.*?)\n\}",
                         features, re.DOTALL)
        assert body, "renderWriteStartRecents not found"
        assert "if (!filtering) items = items.slice" in body.group(1)


class TestWorkspaceComesBackWithTheDocuments:
    def test_the_listing_says_which_workspace_each_document_is_in(self, client):
        made = client.post("/api/notes", json={"title": "Filed", "content": ""}).json()
        listing = client.get("/api/notes").json()
        note = next(n for n in listing if n.get("id") == made["id"])
        assert "workspace" in note and "workspace_name" in note

    def test_it_is_one_query_rather_than_one_per_document(self):
        """A filter on a screen that exists to be fast must not become a query
        per document as the vault grows."""
        from carrot import workspaces

        assert hasattr(workspaces, "workspace_map")
        app_py = (Path(__file__).resolve().parents[1] / "carrot" / "app.py"
                  ).read_text(encoding="utf-8")
        listing = app_py[app_py.index('@app.get("/api/notes")'):]
        listing = listing[:listing.index("@app.get", 10)]
        assert "workspace_map" in listing
        assert "workspace_of" not in listing
