"""The Write tab has a formatting row, and every button on it is wired.

Crepe has always had a selection bubble and a `/` menu, so the editor could
always do bold — but the only row on screen held Send, → Obsidian and Delete,
which are things you do *to* a document rather than *in* one. A document editor
whose toolbar cannot make a heading reads as one that cannot.

The interesting failure here is not a missing button, it is a *dead* one. The
bar drives Milkdown commands, and Milkdown commands only reach the page if the
vendor entry exported them — a bundle rebuild that drops one leaves a button
that looks exactly like the others and does nothing when pressed. So the
contract between the two halves is what these tests hold.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"


def read(path):
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index():
    return read(WEB / "index.html")


@pytest.fixture(scope="module")
def features():
    return read(WEB / "js" / "features.js")


@pytest.fixture(scope="module")
def vendor_entry():
    return read(ROOT / "webvendor" / "src" / "milkdown-entry.js")


@pytest.fixture(scope="module")
def bar_buttons(features):
    """The command name behind every button in the row."""
    rows = re.search(r"NOTE_FORMAT_ROWS\s*=\s*\[(.*?)\n\];", features, re.DOTALL)
    assert rows, "NOTE_FORMAT_ROWS not found"
    # A button is `['cmd', 'glyph', 'title']`. The group names that wrap them
    # are `['history', [`, so requiring a quote after the comma is what keeps
    # 'history' and 'marks' from being read as commands.
    found = re.findall(r"\['([a-zA-Z]+)', '", rows.group(1))
    assert found, "no buttons parsed out of NOTE_FORMAT_ROWS"
    return found


@pytest.fixture(scope="module")
def exported_commands(vendor_entry):
    block = re.search(r"commands:\s*\{(.*?)\n  \},", vendor_entry, re.DOTALL)
    assert block, "the vendor entry exports no commands map"
    return set(re.findall(r"^\s*([a-zA-Z]+):", block.group(1), re.MULTILINE))


class TestTheRowIsOnScreen:
    def test_it_lives_in_write(self, index):
        start = index.index('<section id="view-notes"')
        end = index.index('<section id="view-', start + 1)
        assert 'id="note-format"' in index[start:end]

    def test_there_is_exactly_one_of_it(self, index):
        assert index.count('id="note-format"') == 1

    def test_it_sits_with_the_document_and_not_over_it(self, index):
        """Under the toolbar, above the page. A formatting row that floats over
        the text covers the line you are formatting."""
        assert index.index('id="note-toolbar"') < index.index('id="note-format"')
        assert index.index('id="note-format"') < index.index('id="note-editor-host"')


class TestEveryButtonIsWired:
    """The whole point. A button naming a command the bundle does not export
    is indistinguishable from a working one until it is pressed."""

    def test_every_button_names_an_exported_command(self, bar_buttons, exported_commands):
        missing = [name for name in bar_buttons if name not in exported_commands]
        assert not missing, f"buttons with no command behind them: {missing}"

    @pytest.mark.parametrize("command", ["paragraph", "heading"])
    def test_the_style_picker_commands_are_exported(self, command, exported_commands):
        """These two are reached from the `<select>` rather than a button, so
        they are absent from NOTE_FORMAT_ROWS and would not be caught above."""
        assert command in exported_commands

    def test_the_kit_is_exported_under_a_stable_name(self, vendor_entry, features):
        name = "CarrotMilkdownKit"
        assert f"window.{name}" in vendor_entry
        assert name in features

    def test_the_bar_can_read_the_selection(self, vendor_entry):
        """Lighting a button is a question about the current selection, not a
        command — it needs the view and the schema, not just `commandsCtx`."""
        ctx = re.search(r"ctx:\s*\{([^}]*)\}", vendor_entry)
        assert ctx, "no ctx slices exported"
        assert "editorViewCtx" in ctx.group(1)


class TestItLightsUpForWhatIsActuallyThere:
    def test_strikethrough_is_looked_up_by_its_schema_name(self, features):
        """The schema calls it `strike_through` while the button calls it
        `strikethrough`. Assuming the two agree — `schema.marks[button]` —
        leaves exactly one button permanently dark, and only that one, which
        is the kind of thing that ships."""
        names = re.search(r"NOTE_MARK_NAMES\s*=\s*\{(.*?)\n\};", features, re.DOTALL)
        assert names, "NOTE_MARK_NAMES not found"
        assert "strikethrough: 'strike_through'" in names.group(1)

    def test_the_enclosing_list_is_found_by_walking_out(self, features):
        """The cursor sits in a paragraph inside a list_item inside the list,
        so the node directly above it never says `bullet_list` and the list
        buttons never light."""
        sync = re.search(r"function syncNoteFormatBar\(\)\s*\{(.*?)\n\}", features, re.DOTALL)
        assert sync, "syncNoteFormatBar not found"
        assert "$from.depth" in sync.group(1)

    def test_stored_marks_are_preferred_to_the_document(self, features):
        """With an empty cursor just after Ctrl+B the mark is stored and not
        yet in the document, so reading the document makes the button flick
        off the moment it is pressed."""
        sync = re.search(r"function syncNoteFormatBar\(\)\s*\{(.*?)\n\}", features, re.DOTALL)
        assert "storedMarks" in sync.group(1)


class TestTheGutterKeepsOnlyWhatIsOnlyThere:
    """The handle in the left margin had a `+` and a drag grip. The `+` opened
    the menu `/` opens, which is now also the row's job — so the gutter held a
    third way to do what two other things already did, one of them permanently
    on screen. The grip stays: dragging a block is the one thing the gutter is
    for and the only one with nowhere else to live."""

    def test_the_add_button_is_hidden(self):
        css = (ROOT / "carrot" / "web" / "css" / "style.css").read_text(encoding="utf-8")
        assert ".milkdown-block-handle .operation-item:first-child { display: none; }" in css

    def test_the_drag_grip_is_not(self):
        """Both are `.operation-item`, add then drag, so hiding them by class
        rather than by position would take the grip with it and leave a gutter
        that reacts to the pointer and does nothing."""
        css = (ROOT / "carrot" / "web" / "css" / "style.css").read_text(encoding="utf-8")
        assert ".milkdown-block-handle .operation-item { display: none; }" not in css
        assert ":last-child { display: none; }" not in css


class TestItStopsWhereMarkdownStops:
    """Font, size, colour and alignment are the first things anyone reaches for
    after Word, and none of them survive a save to `.md`. A control that
    silently discards what it sets is worse than an absent one, so the row does
    not offer them — and says so, once, where the font picker would be."""

    @pytest.mark.parametrize("absent", ["fontFamily", "fontSize", "textColor",
                                        "highlight", "alignLeft", "lineSpacing"])
    def test_no_control_that_markdown_cannot_save(self, bar_buttons, absent):
        assert absent not in bar_buttons

    def test_the_row_says_why(self, features):
        assert "fmt-note" in features
        assert "Markdown" in features


class TestItAppearsAndDisappearsWithProse:
    def test_prose_owns_it(self, features):
        """Left out of the mode table it is a row that never hides, which is
        how a formatting bar ends up sitting over a canvas."""
        modes = re.search(r"WRITE_MODES\s*=\s*\{(.*?)\n\};", features, re.DOTALL)
        assert modes, "WRITE_MODES not found"
        prose = re.search(r"prose:\s*\{(.*?)\},", modes.group(1), re.DOTALL)
        assert prose, "no prose mode"
        assert "'note-format'" in prose.group(1)

    def test_showwritemode_does_not_reveal_it(self, features):
        """`owns` but not `reveal`: whether the row can be shown depends on
        whether Milkdown loaded at all, which only mountEditor knows. On the
        textarea fallback every button would be dead."""
        modes = re.search(r"WRITE_MODES\s*=\s*\{(.*?)\n\};", features, re.DOTALL)
        prose = re.search(r"prose:\s*\{(.*?)\},", modes.group(1), re.DOTALL)
        reveal = re.search(r"reveal:\s*\[(.*?)\]", prose.group(1), re.DOTALL)
        assert reveal, "prose declares no reveal list"
        assert "note-format" not in reveal.group(1)

    def test_every_exit_from_mounting_decides_about_the_row(self, features):
        """mountEditor has three endings — no bundle, created, threw — and the
        row is wrong on two of them if only the happy one is handled."""
        mount = re.search(r"async function mountEditor\(markdown\)\s*\{(.*?)\n\}",
                          features, re.DOTALL)
        assert mount, "mountEditor not found"
        assert mount.group(1).count("showNoteFormatBar()") == 3

    def test_a_read_only_document_gets_no_row(self, features):
        """Goals is a view of the database. Buttons that edit it are buttons
        whose writes the server refuses."""
        show = re.search(r"function showNoteFormatBar\(\)\s*\{(.*?)\n\}", features, re.DOTALL)
        assert show, "showNoteFormatBar not found"
        assert "currentNoteReadonly" in show.group(1)


class TestPressingBoldAppliesToTheDocument:
    def test_the_editor_is_focused_before_the_command_runs(self, features):
        """A ProseMirror command applies at the selection, and clicking a
        toolbar button moves focus to the button. Without this the selection is
        stale or gone and half the commands are silent no-ops — the classic
        "the bold button works sometimes"."""
        fmt = re.search(r"async function noteFormat\(cmd\)\s*\{(.*?)\n\}", features, re.DOTALL)
        assert fmt, "noteFormat not found"
        assert ".focus()" in fmt.group(1)

    def test_focus_is_taken_back_after_the_prompt(self, features):
        """Link and image ask for a URL first, and the dialog steals focus on
        its way in — so the two commands that need it most are the two that
        would lose it."""
        fmt = re.search(r"async function noteFormat\(cmd\)\s*\{(.*?)\n\}", features, re.DOTALL)
        assert fmt.group(1).count(".focus()") >= 3

    def test_it_asks_through_the_app_and_not_the_browser(self, features):
        """window.prompt() is disabled in Electron and returns null silently,
        which is a button that does nothing rather than one that fails."""
        fmt = re.search(r"async function noteFormat\(cmd\)\s*\{(.*?)\n\}", features, re.DOTALL)
        assert "inlineTextPrompt" in fmt.group(1)
        assert "window.prompt" not in fmt.group(1)

    def test_formatting_marks_the_document_dirty(self, features):
        """Autosave polls for a changed body, but the poll is on a timer and
        the row can change a document between two ticks."""
        fmt = re.search(r"async function noteFormat\(cmd\)\s*\{(.*?)\n\}", features, re.DOTALL)
        assert "scheduleNoteSave()" in fmt.group(1)
