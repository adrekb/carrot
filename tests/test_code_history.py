"""The Code tab keeps its sessions, and stops leaving them in Chats.

The agent in the Code tab posts to the same `/api/chat/stream` as the chat box,
so its sessions have always been ordinary rows in `conversations`. Two things
followed from nobody marking them:

- they were listed in Chats, so "create a simulation for magnetic fields" sat
  among the things you actually asked Carrot;
- the Code tab, where you spend hours, had no history at all — New task threw
  the session away and there was no way back to one.

app.js has filtered `surface === 'code'` out of the Chats list for a while, but
nothing ever set the marker, so the filter was a no-op. It is set server-side
now, from the `coder` flag — the flag that already decides the shape of the
turn, so any caller that sets it is running a coding turn whether or not it
remembered to say so twice.
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"


def read(*parts):
    return WEB.joinpath(*parts).read_text(encoding="utf-8")


class Request:
    """The fields `_open_conversation` reads off a chat request."""

    def __init__(self, **kwargs):
        self.conversation_id = None
        self.message = "do the thing"
        self.temporary = False
        self.surface = None
        self.coder = False
        self.workspace_id = None
        for name, value in kwargs.items():
            setattr(self, name, value)


@pytest.fixture
def opened(tmp_path, monkeypatch):
    """Open a conversation against a database of its own."""
    monkeypatch.setenv("CARROT_DATA_DIR", str(tmp_path))
    from carrot import app as app_mod, conversation as conv_mod
    from carrot import database

    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "carrot.db"))
    database.init_db()

    def open_one(**kwargs):
        req = Request(**kwargs)
        app_mod._open_conversation(req)
        return conv_mod.get_conversation(req.conversation_id)

    return open_one


class TestACodingTurnMarksItsSession:
    def test_a_coder_turn_is_marked(self, opened):
        conv = opened(coder=True, message="add a retry loop to client.py")
        assert conv["metadata"].get("surface") == "code"

    def test_an_ordinary_turn_is_not(self, opened):
        conv = opened(coder=False, message="what is the news")
        assert "surface" not in conv["metadata"]

    def test_an_explicit_surface_wins(self, opened):
        """The flag fills a gap; it does not overrule a caller that named one."""
        conv = opened(coder=True, surface="agent")
        assert conv["metadata"]["surface"] == "agent"

    def test_the_marker_is_the_shared_constant(self):
        """Two spellings of "code" is the same bug again, one file over."""
        from carrot import conversation as conv_mod

        assert conv_mod.SURFACE_CODE == "code"
        app_py = (ROOT / "carrot" / "app.py").read_text(encoding="utf-8")
        assert "conv_mod.SURFACE_CODE" in app_py

    def test_a_temporary_coding_session_stays_temporary(self, opened):
        """Marking a surface must not quietly file something meant to vanish."""
        conv = opened(coder=True, temporary=True)
        assert conv["metadata"].get("temporary") is True
        assert conv["metadata"].get("surface") == "code"


class TestChatsNoLongerListsThem:
    def test_the_filter_exists(self):
        assert "(c.metadata || {}).surface !== 'code'" in read("js", "app.js")

    def test_the_filter_is_no_longer_dead(self):
        """It has been there all along; what it needed was for something to set
        the marker. This is the assertion that ties the two halves together."""
        app_py = (ROOT / "carrot" / "app.py").read_text(encoding="utf-8")
        assert "getattr(req, \"coder\", False)" in app_py
        assert "surface = conv_mod.SURFACE_CODE" in app_py


class TestTheCodeTabHasAHistory:
    def test_there_is_a_way_to_open_it(self):
        index = read("index.html")
        assert 'onclick="toggleCodeHistory()"' in index
        assert 'id="code-history"' in index

    def test_it_sits_with_new_task(self):
        """Starting a session and getting back to one are the same decision
        taken at the same moment; the tab had only half of it."""
        index = read("index.html")
        assert index.index("toggleCodeHistory()") < index.index("newAgentTask()")

    def test_it_lists_only_coding_sessions(self):
        features = read("js", "features.js")
        listing = re.search(r"async function loadCodeHistory\(\)\s*\{(.*?)\n\}",
                            features, re.DOTALL)
        assert listing, "loadCodeHistory not found"
        assert "surface === 'code'" in listing.group(1)

    def test_opening_one_sets_the_conversation(self):
        """Without this the next message starts a new session and the one you
        just opened is read-only in a way nothing told you about."""
        features = read("js", "features.js")
        opener = re.search(r"async function openCodeSession\(conversationId\)\s*\{(.*?)\n\}",
                           features, re.DOTALL)
        assert opener, "openCodeSession not found"
        assert "agentConversationId = conversationId;" in opener.group(1)

    def test_it_says_what_a_reopened_session_is_missing(self):
        """The trace, plan and diff cards came from stream events that were
        never stored per message. Redrawing the prose alone and saying nothing
        would present a partial record as a whole one."""
        features = read("js", "features.js")
        assert "code-history-note" in features
        assert "not kept" in features

    def test_old_sessions_are_not_guessed_at(self):
        """Sessions from before the marker are in Chats. Inferring which ones
        were coding turns from the tools they happened to call would put
        somebody's private conversation in this list."""
        features = read("js", "features.js")
        assert "are in Chats" in features


class TestTheRailNamesItsSections:
    """Small caps tracked out at 0.08em is a dashboard's voice. The rail is a
    sidebar, and the thing it is actually like sets its headings in the same
    case as the rows under them and tells them apart by weight."""

    @pytest.mark.parametrize("label", [">recent</summary>", "'running now'",
                                       "'needs a look'", ">workspaces</summary>"])
    def test_the_headings_are_lowercase(self, label):
        assert label in read("js", "activity.js")

    def test_the_style_matches(self):
        css = read("css", "style.css")
        head = re.search(r"\.nav-sec-head \{(.*?)\}", css, re.DOTALL)
        assert head, ".nav-sec-head not found"
        assert "text-transform: uppercase" not in head.group(1)
        assert "--w-bold" in head.group(1)

    def test_workspaces_are_in_the_rail(self):
        """The rail answered "what is running" and "what was I just doing" and
        not "what am I doing this inside of"."""
        activity = read("js", "activity.js")
        assert "function activityWorkspaceRow" in activity
        assert "/api/workspaces" in activity

    def test_they_are_not_refetched_on_every_poll(self):
        """The rail polls every four seconds; workspaces change when you make
        one. Polling them on that cadence is a round trip a minute for a list
        that is almost always identical."""
        activity = read("js", "activity.js")
        loader = re.search(r"async function loadActivity\(\)\s*\{(.*?)\n\}", activity, re.DOTALL)
        assert loader, "loadActivity not found"
        assert "/api/workspaces" not in loader.group(1)

    def test_picking_one_makes_it_active(self):
        """Opening without activating would file the next thing you make into
        whichever workspace was active before."""
        activity = read("js", "activity.js")
        row = re.search(r"function activityWorkspaceRow\(workspace\)\s*\{(.*?)\n\}",
                        activity, re.DOTALL)
        assert "setActiveWorkspace" in row.group(1)

def _luminance(rgb):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hex(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def contrast(fg, bg):
    a, b = _luminance(_hex(fg)), _luminance(_hex(bg))
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


class TestTheActiveWorkspaceRowIsReadable:
    """"0 items" on the active row was `--faint` on `--accent-fill`: a mid grey
    picked to be quiet on a dark card, sitting on orange. **1.11:1** — not dim,
    invisible. The row sets `--on-accent` for exactly this reason and the meta
    line was hard-setting a colour straight over it."""

    def test_the_meta_takes_the_rows_ink(self):
        css = read("css", "style.css")
        assert ".ws-row.active .ws-row-meta { color: var(--on-accent); }" in css

    def test_it_is_not_dimmed(self):
        """The obvious move — full ink at reduced alpha — fails, and only
        measuring shows it. Every `--accent-fill`/`--on-accent` pair here is
        tuned to *just* clear AA, so there is no headroom to spend: ember is
        4.72:1, and 0.9 alpha takes it to 4.29."""
        css = read("css", "style.css")
        rule = re.search(r"\.ws-row\.active \.ws-row-meta \{([^}]*)\}", css)
        assert rule, "rule not found"
        assert "opacity" not in rule.group(1)

    @pytest.mark.parametrize("fill,ink", [
        ("#e0620f", "#1a1208"),   # carrot
        ("#e8471f", "#1a1208"),   # ember — the tightest pair, and the one that decides it
        ("#e09000", "#1a1208"),   # amber
        ("#8e2fd0", "#ffffff"),   # orchid
        ("#0a9b80", "#0a1a16"),   # teal
        ("#1f55dd", "#ffffff"),   # indigo
    ])
    def test_every_accent_clears_aa_at_full_ink(self, fill, ink):
        assert contrast(ink, fill) >= 4.5

    @pytest.mark.parametrize("fill,ink", [("#e8471f", "#1a1208")])
    def test_dimming_ember_would_not(self, fill, ink):
        """The measurement this rule exists because of, kept so that anyone
        re-adding an opacity has to delete an assertion that says why not."""
        blended = tuple(round(i * 0.9 + f * 0.1)
                        for i, f in zip(_hex(ink), _hex(fill)))
        faded = "#%02x%02x%02x" % blended
        assert contrast(faded, fill) < 4.5

    def test_the_old_value_was_the_bug(self):
        """`--faint`, the value that was there, on the fill it was there over."""
        assert contrast("#96907f", "#e0620f") < 1.5


class TestTheRailDoesNotLoseRecents:
    """/api/workspaces is one small table and /api/activity is four queries, so
    the workspaces fetch usually wins the race. Redrawing on the way past
    rebuilt the rail from an activityData that had not arrived — a workspaces
    section and nothing else, which reads exactly like recents being removed,
    and stays that way if the activity call then fails."""

    def test_the_workspace_fetch_waits_for_activity(self):
        activity = read("js", "activity.js")
        loader = re.search(r"async function loadActivityWorkspaces\(\)\s*\{(.*?)\n\}",
                           activity, re.DOTALL)
        assert loader, "loadActivityWorkspaces not found"
        assert "if (activityLoaded) renderActivity();" in loader.group(1)

    def test_loaded_is_distinct_from_empty(self):
        """An empty rail and an unloaded one look the same and mean opposite
        things, so the flag cannot be `recent.length`."""
        activity = read("js", "activity.js")
        assert "let activityLoaded = false;" in activity
        assert "activityLoaded = true;" in activity

    def test_recents_still_render_after_workspaces(self):
        """Order matters and so does the early return: the workspaces block has
        to be appended before `if (!recent.length) return;`, or a rail with no
        recents loses the section that does have something in it."""
        activity = read("js", "activity.js")
        assert (activity.index("activityWorkspaceRow(workspace)")
                < activity.index("if (!recent.length) return;"))
