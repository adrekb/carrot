"""A group: a marked region of a document that carries its own route.

The interesting history here is a *layer* mistake rather than a logic one. The
first version drew a group's chip by putting classes on the editor's own nodes
and hiding the marker text with CSS. It rendered, and then vanished roughly
120ms later with nothing typed in between: ProseMirror rebuilds its DOM from
its document state and discards anything it did not put there. No retry loop
fixes that — it is a race against the editor that the editor always wins.

So the chip is a ProseMirror decoration now, and most of what is worth holding
in a test is the seam between the three pieces that has to line up for one to
appear at all:

  * the vendor bundle has to *export* the decoration primitives — a rebuild
    that drops one leaves a Write tab that looks fine and never draws a chip;
  * the editor has to register the plugin *before* `create()`, because a
    Milkdown plugin is added to an editor being built, not to a running one;
  * the CSS has to outrank Crepe's own `.milkdown .ProseMirror p`, which it
    only does with `.milkdown` in front — and losing that fight is silent, and
    only for the properties Crepe also sets.

The other half is the format, which is the part that has to survive being
edited by a person: a group lives in the file as a pair of HTML comments, and
its route is written as the very `@/to`, `@/model` and `@/file` directives a
whole note already uses. That is what the last group of tests holds — a group
and a note reach the same router by the same path.
"""
import re
from pathlib import Path

import pytest

from carrot import doc_agent

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"


def read(path):
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def groups_js():
    return read(WEB / "js" / "docgroups.js")


@pytest.fixture(scope="module")
def features_js():
    return read(WEB / "js" / "features.js")


@pytest.fixture(scope="module")
def index_html():
    return read(WEB / "index.html")


@pytest.fixture(scope="module")
def style_css():
    return read(WEB / "css" / "style.css")


@pytest.fixture(scope="module")
def vendor_entry():
    return read(ROOT / "webvendor" / "src" / "milkdown-entry.js")


@pytest.fixture(scope="module")
def vendor_bundle():
    return read(WEB / "vendor" / "milkdown.js")


class TestTheChipIsOwnedByTheEditor:
    """The whole point of the rewrite. Any of these failing means the chip is
    being drawn *at* ProseMirror again rather than *by* it."""

    def test_the_decoration_primitives_cross_the_bundle_boundary(self, vendor_entry):
        block = re.search(r"window\.CarrotMilkdownKit = \{(.*?)\n\};", vendor_entry, re.DOTALL)
        assert block, "the vendor entry exports no kit"
        exported = block.group(1)
        assert "$prose" in exported, "no $prose: a bare ProseMirror plugin cannot be registered"
        for name in ("Plugin", "PluginKey", "Decoration", "DecorationSet"):
            assert name in exported, f"the kit does not export {name}"

    def test_the_built_bundle_actually_carries_them(self, vendor_bundle):
        """The entry is the source; this is the file the browser loads. They
        drift the moment somebody edits one without running the build."""
        assert "$prose" in vendor_bundle
        assert "DecorationSet" in vendor_bundle

    def test_the_chip_is_a_widget_decoration(self, groups_js):
        assert "Decoration.widget" in groups_js
        assert "Decoration.node" in groups_js

    def test_nothing_reaches_into_the_editors_dom(self, groups_js):
        """A MutationObserver reapplying classes is the shape of the bug this
        replaced: it races the editor's own redraw and loses."""
        # `new MutationObserver`, not the bare word: the header explains at
        # length why there is no longer one, and a test that cannot tell an
        # explanation from a use forces the explanation to be deleted.
        assert "new MutationObserver" not in groups_js
        assert "decorateGroups" not in groups_js
        assert "watchGroups" not in groups_js

    def test_the_editor_registers_it_before_it_is_created(self, features_js):
        """`editor.use()` is how a plugin joins an editor being built. After
        `create()` it is too late, and the chip never appears."""
        mount = features_js[features_js.index("async function mountEditor"):]
        mount = mount[:mount.index("\n}\n")]
        install = mount.index("installGroupPlugin")
        create = mount.index("crepeInstance.create()")
        assert install < create, "the plugin is registered after the editor is created"

    def test_the_module_is_loaded(self, index_html):
        assert '<script src="/js/docgroups.js"></script>' in index_html


class TestTheMarkerIsReadFromTheDocumentNotTheDom:
    """Milkdown parses a block HTML comment into a paragraph wrapping an `html`
    atom that keeps its source in `attrs.value`. `node.textContent` on that is
    the empty string, so the obvious reading finds no markers at all."""

    def test_it_asks_the_html_node_for_its_value(self, groups_js):
        assert "attrs.value" in groups_js

    def test_the_opening_pattern_is_lazy(self, groups_js):
        """Greedy matching ate the closing `--` and then had nothing left to
        match `-->` with, so no marker was ever recognised."""
        pattern = re.search(r"const GROUP_OPEN = (/.*?/);", groups_js)
        assert pattern, "GROUP_OPEN not found"
        assert "(.*?)" in pattern.group(1)


class TestTheHighlightSurvivesCrepesOwnStyles:
    def test_the_group_rules_outrank_the_editors(self, style_css):
        """`.ProseMirror .cg-body` loses to `.milkdown .ProseMirror p` on
        specificity. It lost silently, and only for padding — which is exactly
        the kind of failure nobody reads a stylesheet to find."""
        block = style_css[style_css.index("/* ===== Groups ====="):style_css.index(".group-menu {")]
        selectors = re.findall(r"^(\.\S[^{]*)\{", block, re.MULTILINE)
        assert selectors, "no group selectors found"
        for selector in selectors:
            for part in selector.split(","):
                part = part.strip()
                if not part:
                    continue
                assert part.startswith(".milkdown "), f"{part} does not outrank Crepe's own p rule"

    def test_the_gap_a_group_cancels_is_the_gap_the_editor_sets(self, style_css):
        """The document is a flex column, so three blocks that each paint a
        background come out as three stripes — the space between them belongs
        to the container, not to them. Cancelling it means knowing the value,
        so it is a token declared next to the `gap` it feeds."""
        editor = re.search(r"\.editor \{(.*?)\n\}", style_css, re.DOTALL)
        assert editor, ".editor rule not found"
        assert "--doc-block-gap" in editor.group(1)
        assert "gap: var(--doc-block-gap)" in editor.group(1)
        assert "calc(-1 * var(--doc-block-gap" in style_css


class TestWhatTheChipSays:
    def test_it_names_where_the_model_runs(self, groups_js):
        """Local or not is the one thing about a route you cannot read off its
        name, and the reason the chip had to be a real element."""
        assert "🖥" in groups_js and "☁" in groups_js
        assert "'local'" in groups_js

    def test_an_unpinned_group_still_names_its_model(self, groups_js):
        """A group with no `model=` is not a group with no model — it runs on
        whatever the app is set to. A blank there reads as unanswered."""
        assert "currentModel" in groups_js
        assert "cg-inherited" in groups_js

    def test_a_changed_model_reaches_the_open_document(self, groups_js):
        """The inherited half of the label lives outside the document, so a
        document change is not the only reason to redraw."""
        assert "function refreshGroupChips" in groups_js
        assert "setMeta" in groups_js

    def test_the_app_tells_it_when_the_model_changes(self):
        app_js = read(WEB / "js" / "app.js")
        assert app_js.count("refreshGroupChips()") == 2, (
            "both model pickers should nudge the chips"
        )

    def test_it_is_redrawn_when_what_it_says_changes(self, groups_js):
        """Widgets are reused across redraws when their key matches, which is
        what keeps the click handlers alive — and what leaves a chip showing
        the route it had before, if the key does not cover the label."""
        key = re.search(r"key: \[(.*?)\]", groups_js, re.DOTALL)
        assert key, "the widget has no key"
        for field in ("info.where", "info.files.length", "info.model"):
            assert field in key.group(1)


class TestEditingAGroupKeepsWhatItDidNotTouch:
    def test_the_marker_is_built_in_one_place(self, groups_js):
        """Rebuilding it from the two fields the menu knew about silently
        dropped a group's cited files whenever its destination changed."""
        assert "function groupMarkerLine" in groups_js
        assert groups_js.count("`<!--carrot:group") == 1

    def test_editing_starts_from_what_the_group_already_says(self, groups_js):
        assert "{ ...group.attrs }" in groups_js

    def test_paths_are_encoded_because_an_attribute_ends_at_a_space(self, groups_js):
        assert "encodeURIComponent" in groups_js
        assert "decodeURIComponent" in groups_js


class TestAGroupTravelsAsTheDirectivesTheFormatAlreadySpeaks:
    """No second router: a group writes `@/to`, `@/model` and `@/file` lines
    and `/api/doc/send` resolves them by the path a whole note takes."""

    def test_the_send_path_writes_all_three(self, groups_js):
        send = groups_js[groups_js.index("async function sendGroup"):]
        send = send[:send.index("\n}\n")]
        assert "@/to/" in send
        assert "@/model/" in send
        assert "@/file/" in send

    def test_a_groups_route_resolves(self, isolated_db):
        resolved = doc_agent.resolve(
            "on-device speech separation\n\n@/to/research/deep\n@/model/local/phi4:14b"
        )
        assert (resolved.destination, resolved.option) == ("research", "deep")
        assert resolved.route is not None
        assert resolved.route.model == "phi4:14b"

    def test_a_group_and_a_whole_note_reach_the_same_route(self, isolated_db):
        """The point of writing the route back into the text rather than into a
        second payload field: there is one resolver, so they cannot drift."""
        as_group = doc_agent.resolve("body\n\n@/to/research/deep\n@/model/local/phi4:14b")
        as_note = doc_agent.resolve("@/to/research/deep @/model/local/phi4:14b body")
        assert (as_group.destination, as_group.option) == (as_note.destination, as_note.option)
        assert as_group.route.model == as_note.route.model

    def test_a_cited_file_is_read_at_send_time(self, isolated_db, tmp_path, monkeypatch):
        from carrot import agent_tools

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        (tmp_path / "router.py").write_text("MARKER_FROM_THE_CITED_FILE", encoding="utf-8")
        resolved = doc_agent.resolve("what does this do?\n\n@/to/chat\n@/file/router.py")
        assert "MARKER_FROM_THE_CITED_FILE" in resolved.context

    def test_a_path_with_a_space_survives_the_round_trip(self, isolated_db, tmp_path, monkeypatch):
        """The reason the attribute is percent-encoded and the directive is
        quoted: half the files worth citing have a space in the name."""
        from carrot import agent_tools

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        (tmp_path / "my paper.md").write_text("MARKER_WITH_A_SPACE", encoding="utf-8")
        resolved = doc_agent.resolve('read it\n\n@/to/chat\n@/file/"my paper.md"')
        assert "MARKER_WITH_A_SPACE" in resolved.context
