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
    """The blanket filter is gone — see TestTheHistoryMenuHoldsBoth. Marking
    them is what mattered; what the marker is *used* for changed once the menu
    could label a code session instead of hiding it."""

    def test_the_marker_is_what_the_menu_sorts_on(self):
        assert "(c.metadata || {}).surface === 'code'" in read("js", "app.js")

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

    def test_the_tool_path_comes_back_with_it(self):
        """I shipped this replaying the prose only, with a note saying the
        trace and diffs were not kept. That was wrong: every turn stores its
        trace on the assistant row, because the Code tab posts to the same
        endpoint as chat and chat has replayed its traces all along. The events
        were on disk and nothing was reading them."""
        features = read("js", "features.js")
        assert "function replayAgentTrace" in features
        opener = re.search(r"async function openCodeSession\(conversationId\)\s*\{(.*?)\n\}",
                           features, re.DOTALL).group(1)
        assert "replayAgentTrace(wrap," in opener

    def test_the_note_claiming_otherwise_is_gone(self):
        """The sentence, not the phrase — the comment above `replayAgentTrace`
        quotes the old claim in order to say it was wrong, and a test that
        cannot tell those apart forbids explaining the fix."""
        features = read("js", "features.js")
        assert "the tool trace and diffs from this session are not kept" not in features
        assert "code-history-note" not in features

    def test_it_is_drawn_with_the_live_functions(self):
        """A second renderer is a second thing to keep in step, and the way you
        find out it drifted is a reopened session that does not look like the
        one you watched."""
        features = read("js", "features.js")
        replay = re.search(r"function replayAgentTrace\(wrap, trace\)\s*\{(.*?)\n\}",
                           features, re.DOTALL).group(1)
        for shared in ("agentToolCard(", "agentToolCardResult(", "agentTrace(", "CARD_TOOLS"):
            assert shared in replay, shared

    def test_the_trace_sits_above_the_answer(self):
        """It is what the turn did on the way to saying this; reading it
        afterwards is reading it in the wrong order."""
        features = read("js", "features.js")
        opener = re.search(r"async function openCodeSession\(conversationId\)\s*\{(.*?)\n\}",
                           features, re.DOTALL).group(1)
        assert opener.index("replayAgentTrace(wrap,") < opener.index("wrap.appendChild(body)")

    def test_a_clipped_result_says_so_on_its_own_card(self):
        """Results are stored cut to 400 characters, so a page of test output
        comes back as its first paragraph. A note under the whole session would
        claim that of every card, including the ones stored whole."""
        features = read("js", "features.js")
        assert "function markReplayedResult" in features
        assert "400" in features

    def test_the_server_keeps_what_the_replay_needs(self):
        """The other half of the contract. Dropping `tool` or `tool_result`
        from the stored events would empty this without breaking anything that
        would fail loudly."""
        app_py = (ROOT / "carrot" / "app.py").read_text(encoding="utf-8")
        kept = re.search(r"TRACE_EVENTS = \((.*?)\)", app_py, re.DOTALL).group(1)
        for event in ('"tool"', '"tool_result"', '"plan"'):
            assert event in kept, event

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

    @pytest.mark.parametrize("label", [">recent</summary>", "'in progress'",
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

    def test_no_section_can_early_return_past_another(self):
        """The first version returned early when there were no recents, which
        put every section after that one at the mercy of the one before it.
        Each is its own `if` now — see TestTheRailIsAGlanceNotAList for the
        order they run in."""
        activity = read("js", "activity.js")
        assert "if (!recent.length) return;" not in activity

class TestRecentsKnowWhichAreCode:
    """A chat session and a coding session are the same row from the same
    table, they read identically at 12px, and they open in different tabs."""

    @pytest.mark.parametrize("metadata,expected", [
        ('{"surface": "code"}', "code"),
        ('{"surface": "agent"}', "conversation"),
        ("{}", "conversation"),
        (None, "conversation"),
        ("not json at all", "conversation"),
    ])
    def test_the_kind_is_read_off_the_metadata(self, metadata, expected):
        from carrot import activity

        assert activity._surface_kind(metadata) == expected

    def test_broken_metadata_costs_one_row_its_icon_not_the_panel(self):
        """The rail is polled. A row with an unparseable blob must not raise
        through `recent()` and empty the whole thing."""
        from carrot import activity

        assert activity._surface_kind("{{{") == "conversation"

    def test_the_query_selects_metadata(self):
        """Without the column there is nothing to classify from, and every
        session comes back as a conversation."""
        source = (ROOT / "carrot" / "activity.py").read_text(encoding="utf-8")
        assert "SELECT id, title, updated_at, metadata FROM conversations" in source


class TestTheRailIsAGlanceNotAList:
    def test_the_caps_are_three_and_five(self):
        activity = read("js", "activity.js")
        assert "const RAIL_WORKSPACES = 3;" in activity
        assert "const RAIL_RECENTS = 5;" in activity

    def test_both_sections_are_sliced(self):
        activity = read("js", "activity.js")
        assert "activityWorkspaces.slice(0, RAIL_WORKSPACES)" in activity
        assert "recent.slice(0, RAIL_RECENTS)" in activity

    def test_there_is_a_way_past_the_cap(self):
        activity = read("js", "activity.js")
        assert "function activityMoreRow" in activity
        assert "see more" in activity
        assert "see all" in activity

    def test_see_more_does_not_reuse_the_other_nav_more(self):
        """`.nav-more` is already a component in this stylesheet — an uppercase
        tracked-out disclosure — so reusing the name made these rows shout
        "SEE ALL 5" in the middle of a lowercase rail."""
        activity = read("js", "activity.js")
        assert "'nav-recent nav-seemore'" in activity
        assert "'nav-recent nav-more'" not in activity
        css = read("css", "style.css")
        assert ".nav-seemore .nav-recent-label" in css

    def test_a_code_row_is_marked(self):
        activity = read("js", "activity.js")
        row = re.search(r"function activityRecentRow\(item\)\s*\{(.*?)\n\}",
                        activity, re.DOTALL)
        assert row, "activityRecentRow not found"
        assert "&lt;/&gt;" in row.group(1)
        assert "item.kind === 'code'" in row.group(1)

    def test_a_code_row_opens_in_the_code_tab(self):
        """Sending it to chat renders it as a transcript in a tab with no file
        tree, no diff and no agent — the same conversation, and not the thing
        that was clicked."""
        activity = read("js", "activity.js")
        assert "code: (id) => {" in activity
        assert "switchTab('code');" in activity

    def test_in_progress_comes_last(self):
        """It is the only section that moves, and a moving thing at the foot of
        a still rail is not missed. What the top cost was pushing the two
        sections you navigate with down the page whenever something ran."""
        activity = read("js", "activity.js")
        render = re.search(r"function renderActivity\(\)\s*\{(.*?)\n\}",
                           activity, re.DOTALL).group(1)
        assert render.index("activityWorkspaceRow") < render.index("activityRecentRow")
        assert render.index("activityRecentRow") < render.index("nav-running")

    def test_it_is_called_in_progress(self):
        assert "'in progress'" in read("js", "activity.js")


class TestTheHistoryMenuHoldsBoth:
    def test_code_sessions_are_no_longer_filtered_out(self):
        """Right while they had nowhere else to be listed; wrong now that this
        is *the* history. A history that silently omits half your week is worse
        than one that mixes two kinds."""
        app_js = read("js", "app.js")
        assert ".filter(c => (c.metadata || {}).surface !== 'code')" not in app_js

    def test_they_are_their_own_kind(self):
        app_js = read("js", "app.js")
        assert "(c.metadata || {}).surface === 'code' ? 'code' : 'chat'" in app_js

    def test_there_is_a_code_filter_chip(self):
        assert 'data-kind="code"' in read("index.html")

    def test_the_rows_are_labelled(self):
        app_js = read("js", "app.js")
        assert "history-tag" in app_js
        assert "&lt;/&gt;" in app_js

    def test_the_dot_has_a_colour_of_its_own(self):
        """Two kinds were the accent and the green; a third that is either of
        those cannot be told from them at 6px."""
        css = read("css", "style.css")
        assert ".history-dot.chip-code { background: var(--yellow); }" in css

    def test_opening_one_goes_to_the_code_tab(self):
        app_js = read("js", "app.js")
        opener = re.search(r"function openHistoryItem\(kind, id\)\s*\{(.*?)\n\}",
                           app_js, re.DOTALL).group(1)
        assert "if (kind === 'code')" in opener
        assert opener.index("kind === 'code'") < opener.index("switchTab('workspace')")


class TestTheBlankScreenSaysWhatYouWereDoing:
    def test_the_line_exists(self):
        assert 'id="chat-resume"' in read("index.html")

    def test_it_is_absent_rather_than_empty(self):
        """A row that says "nothing yet" teaches people to stop reading the
        space it occupies."""
        activity = read("js", "activity.js")
        resume = re.search(r"function renderChatResume\(\)\s*\{(.*?)\n\}",
                           activity, re.DOTALL).group(1)
        assert "host.classList.add('hidden')" in resume

    def test_running_work_wins_over_a_finished_thing(self):
        """"Research is going" is a reason to wait or watch; "you were reading
        X" is only a way back."""
        activity = read("js", "activity.js")
        resume = re.search(r"function renderChatResume\(\)\s*\{(.*?)\n\}",
                           activity, re.DOTALL).group(1)
        assert resume.index("running.length") < resume.index("else if (recent)")

    def test_it_costs_no_extra_request(self):
        """It is drawn from the poll the rail already makes."""
        activity = read("js", "activity.js")
        resume = re.search(r"function renderChatResume\(\)\s*\{(.*?)\n\}",
                           activity, re.DOTALL).group(1)
        assert "api(" not in resume
        assert "renderChatResume();" in activity

    def test_the_live_dot_survives_reduced_motion(self):
        """It is the only thing on screen saying something is still happening,
        and a frozen live indicator is indistinguishable from a dead one."""
        css = read("css", "style.css")
        assert ".chat-resume-pulse" in css
        # The exceptions are listed on one line with the other live dots.
        assert ".nav-job-pulse, .icon-btn.recording, .chat-resume-pulse {" in css
