"""When the chips cannot be drawn, the document shows its own syntax.

Reported with a screenshot of a note reading, in full:

    <!--carrot:group to=chat model=local/gemma4:e4b-->
    testing the routing features.
    <!--/carrot:group-->

and the words "illegible / this happens on my machine don't know why, I did
just install the new build as well, newest".

Nothing is *wrong* in that document. The markers are correct, they still route
correctly when sent, and the same file draws a chip perfectly well against the
checkout. What failed is the layer that hides them: the chip is a ProseMirror
decoration registered from `window.CarrotMilkdownKit`, and `installGroupPlugin`
returns `false` — silently — when that bundle predates the exports it needs. A
desktop build carrying an older vendor bundle therefore shows every group as
raw comment syntax, with nothing anywhere saying so.

Silence is the bug being fixed here. The reason is recorded and shown, and the
reader is offered a way out of a document they cannot read.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"


@pytest.fixture(scope="module")
def groups_js():
    return (WEB / "js" / "docgroups.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def features_js():
    return (WEB / "js" / "features.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html():
    return (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def style_css():
    return (WEB / "css" / "style.css").read_text(encoding="utf-8")


def body_of(source, name):
    start = source.index(f"function {name}(")
    rest = source[start:]
    end = rest.find("\nfunction ", 1)
    tail = rest.find("\nasync function ", 1)
    if tail != -1 and (end == -1 or tail < end):
        end = tail
    return rest if end == -1 else rest[:end]


class TestTheFailureIsRecorded:
    """It returned a bare `false` from four different causes, and the caller
    threw that away."""

    def test_each_cause_says_which_one_it_was(self, groups_js):
        install = body_of(groups_js, "installGroupPlugin")
        assert install.count("groupPluginProblem =") >= 4
        assert "the editor did not start" in install
        assert "the editor bundle did not load" in install
        assert "exports no decoration hooks" in install
        assert "refused the chip plugin" in install

    def test_the_bundle_being_too_old_is_named_as_such(self, groups_js):
        # The one a desktop user actually hits: everything looks fine and no
        # chip is ever drawn.
        install = body_of(groups_js, "installGroupPlugin")
        assert "older than group chips" in install

    def test_success_clears_it(self, groups_js):
        # Otherwise a note opened after a failed one inherits the complaint.
        install = body_of(groups_js, "installGroupPlugin")
        assert "groupPluginProblem = '';" in install

    def test_it_still_refuses_to_take_the_editor_down(self, groups_js):
        # The rest of the Write tab works without a chip; throwing here would
        # lose the editor as well as the decoration.
        install = body_of(groups_js, "installGroupPlugin")
        assert "catch (exc)" in install
        assert "return false;" in install


class TestTheDocumentSaysWhatHappened:

    def test_the_check_compares_markers_against_chips(self, groups_js):
        # Not "did the install succeed". A plugin that registered and then drew
        # nothing fails identically from where the user sits.
        check = body_of(groups_js, "checkGroupChips")
        assert "groupsInDocument().length" in check
        assert "querySelectorAll('.cg-chip').length" in check

    def test_a_healthy_document_says_nothing(self, groups_js):
        check = body_of(groups_js, "checkGroupChips")
        assert "if (!markers || chips)" in check
        assert "strip.classList.add('hidden')" in check

    def test_it_says_the_routes_still_work(self, groups_js):
        # The markers are correct and `/api/doc/send` reads them. Somebody
        # looking at raw syntax has no way to know that.
        check = body_of(groups_js, "checkGroupChips")
        assert "still route correctly" in check

    def test_it_reports_the_recorded_reason(self, groups_js):
        check = body_of(groups_js, "checkGroupChips")
        assert "groupPluginProblem" in check

    def test_it_is_asked_after_the_editor_has_painted(self, features_js):
        # The chips are decorations drawn by the editor, so asking before its
        # first render would report every document as broken.
        assert "setTimeout(checkGroupChips, 250)" in features_js

    def test_no_editor_open_is_not_a_complaint(self, groups_js):
        check = body_of(groups_js, "checkGroupChips")
        assert "catch (_)" in check


class TestThereIsAWayOut:

    def test_the_markers_can_be_taken_out(self, groups_js):
        assert "async function removeAllGroupMarkers(" in groups_js
        remove = body_of(groups_js, "removeAllGroupMarkers")
        # One definition of what a marker is — the same function the composer
        # uses when staging a document.
        assert "stripGroupMarkers(getEditorMarkdown())" in remove
        assert "mountEditor(cleaned)" in remove
        assert "scheduleNoteSave()" in remove

    def test_it_says_what_it_costs(self, groups_js):
        # Their routes go with them. That is the honest trade, and the
        # alternative is a document you cannot read.
        remove = body_of(groups_js, "removeAllGroupMarkers")
        assert "routes went with them" in remove

    def test_the_button_is_wired(self, groups_js):
        check = body_of(groups_js, "checkGroupChips")
        assert 'data-act="strip"' in check
        assert "removeAllGroupMarkers()" in check


class TestItHasSomewhereToBeDrawn:

    def test_the_strip_is_in_the_document_column(self, index_html):
        assert 'id="doc-note"' in index_html
        # Above the batch strip and the reference bar, under the toolbar.
        assert index_html.index('id="doc-note"') < index_html.index('id="doc-batch"')

    def test_it_starts_hidden(self, index_html):
        assert 'id="doc-note" class="doc-note hidden"' in index_html

    def test_it_is_styled(self, style_css):
        for selector in (".doc-note {", ".doc-note.hidden", ".doc-note-act"):
            assert selector in style_css, f"{selector} is drawn by nothing"


class TestTheBundleInThisCheckoutIsNotTheProblem:
    """The exports exist here, so a build showing raw markers is carrying an
    older vendor file than this one — which is what the message says."""

    def test_the_kit_exports_what_the_plugin_needs(self):
        bundle = (WEB / "vendor" / "milkdown.js").read_text(encoding="utf-8")
        at = bundle.index("window.CarrotMilkdownKit=")
        block = bundle[at:at + 600]
        for export in ("$prose", "prose:", "Decoration", "DecorationSet",
                       "Plugin", "PluginKey"):
            assert export in block, f"the vendor bundle exports no {export}"

    def test_the_marker_from_the_report_is_one_the_parser_accepts(self, groups_js):
        # `to=chat model=local/gemma4:e4b`, exactly as it appeared on screen.
        pattern = re.search(r"const GROUP_OPEN = (/.*/);", groups_js).group(1)
        body, _, flags = pattern[1:].rpartition("/")
        assert re.match(_js_regex_to_python(body),
                        "<!--carrot:group to=chat model=local/gemma4:e4b-->")
        closing = re.search(r"const GROUP_CLOSE = /(.*)/;", groups_js).group(1)
        assert re.match(_js_regex_to_python(closing), "<!--/carrot:group-->")


def _js_regex_to_python(source: str) -> str:
    """These two patterns use only syntax the modules share."""
    return source
