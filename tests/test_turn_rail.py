"""One tick per question, down the right edge of the transcript.

A long conversation is a scrollbar and nothing else. You know the thing you
want is "somewhere around when I asked about friction", and the only way back
is to drag and read — which gets worse the longer the answers are, and Carrot's
answers are long.

Collapsed the rail is a column of dashes; hovered it is the questions.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"
JS = (WEB / "js" / "turnrail.js").read_text(encoding="utf-8")
CSS = (WEB / "css" / "style.css").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")


def code():
    """The JavaScript with its comments removed.

    A test that greps the whole file cannot tell the code from the comment
    explaining why the code is the way it is — and forbidding a string in the
    comments forbids explaining the fix.
    """
    return "\n".join(line.split("//", 1)[0] for line in JS.splitlines())


def rule(selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, f"no rule for {selector}"
    return match.group(1)


class TestItIsLoaded:
    def test_the_script_ships(self):
        assert '<script src="/js/turnrail.js"></script>' in INDEX


class TestItTracksQuestions:
    def test_it_ticks_questions_not_messages(self):
        """An answer is where the turn went; you navigate by what you asked."""
        assert ".message.user" in JS
        assert ".message.assistant" not in code()

    def test_one_question_is_not_navigation(self):
        assert "TURN_RAIL_MIN = 2" in JS
        render = re.search(r"function renderTurnRail\(\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "TURN_RAIL_MIN" in render
        assert "add('hidden')" in render

    def test_the_label_is_the_first_line(self):
        """A question with a pasted stack trace under it should read as the
        question, not as the first line of the trace."""
        render = re.search(r"function renderTurnRail\(\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "split('\\n')[0]" in render

    def test_it_reads_the_source_not_the_rendering(self):
        """`dataset.raw` is what was actually typed. `textContent` on a
        rendered bubble picks up the timestamp and the action buttons."""
        assert "dataset.raw" in JS


class TestTheActiveTick:
    def test_it_is_the_last_question_above_the_middle(self):
        """Not the nearest, which flickers between two ticks as a long answer
        crosses the midpoint; and not the first visible, which jumps to the
        next question the moment its first line appears while you are still
        reading the previous answer."""
        body = re.search(r"function markTurnRailActive\(\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "clientHeight / 2" in body
        assert "<= middle" in body

    def test_it_does_not_rewrite_the_dom_every_scroll_event(self):
        """This runs on every scroll frame of a long transcript."""
        body = re.search(r"function markTurnRailActive\(\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "if (active === turnRailActive) return;" in body

    def test_scrolling_is_throttled_to_a_frame(self):
        assert "requestAnimationFrame" in JS

    def test_the_current_turn_is_marked_by_length_as_well_as_colour(self):
        """Findable at a glance down a column of twenty, which colour alone on
        a 2px dash is not."""
        assert "width: 20px" in rule(".turn-tick.on .turn-tick-mark")
        assert "width: 14px" in rule(".turn-tick-mark")


class TestJumping:
    def test_it_scrolls_the_container_itself(self):
        """`scrollIntoView({behavior: 'smooth'})` is a silent no-op on this
        container — measured: `auto` moves it 2278px and `smooth` moves it
        zero, while `scrollTo({behavior: 'smooth'})` on the scroller works."""
        assert "scrollIntoView" not in code()
        body = re.search(r"function scrollToTurn\(index\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "scroller.scrollTo(" in body
        assert "behavior: 'smooth'" in body

    def test_it_leaves_air_above_the_question(self):
        """Pinned flush to the top edge, a question reads as clipped."""
        assert "TURN_RAIL_HEADROOM" in JS
        body = re.search(r"function scrollToTurn\(index\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "TURN_RAIL_HEADROOM" in body

    def test_it_cannot_scroll_past_the_top(self):
        body = re.search(r"function scrollToTurn\(index\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "Math.max(0," in body


class TestItStaysInStepWithTheTranscript:
    def test_it_watches_rather_than_being_told(self):
        """There are a dozen places that change the transcript, and the one
        that gets forgotten is the one that leaves a rail pointing at a
        conversation you have left."""
        assert "MutationObserver" in JS
        assert "childList: true" in JS

    def test_the_observer_is_coalesced(self):
        """A streaming answer mutates its node many times a second and none of
        those change the list of questions."""
        watcher = re.search(r"function watchTurnRail\(\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "clearTimeout(pending)" in watcher

    def test_it_is_bound_once(self):
        watcher = re.search(r"function watchTurnRail\(\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "dataset.railWatched" in watcher


class TestItDoesNotScrollAway:
    def test_it_is_a_sibling_of_the_transcript(self):
        """Inside the scroller it would scroll away with the text it is there
        to navigate."""
        body = re.search(r"function ensureTurnRail\(\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "scroller.parentElement.appendChild" in body

    def test_the_column_holding_both_positions_it(self):
        """Without this it anchors to the page and sits over the sidebar on a
        narrow window."""
        assert "#ws-center { position: relative; }" in CSS
        assert "position: absolute" in rule(".turn-rail")


class TestItIsQuietUntilWanted:
    def test_the_labels_are_hidden_until_hover(self):
        assert "display: none" in rule(".turn-tick-label")
        assert ".turn-rail:hover .turn-tick-label { display: block; }" in CSS

    def test_the_label_is_in_the_dom_either_way(self):
        """It is what a screen reader reads and what the tick means. Building
        it on hover would make the rail unreadable to anyone not using a
        pointer."""
        render = re.search(r"function renderTurnRail\(\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "turn-tick-label" in render

    def test_the_rail_is_labelled_for_a_screen_reader(self):
        assert "aria-label" in JS

    def test_the_full_question_is_the_tooltip(self):
        """The label is one truncated line; the tooltip is what you actually
        asked."""
        render = re.search(r"function renderTurnRail\(\)\s*\{(.*?)\n\}", JS, re.DOTALL).group(1)
        assert "title=" in render


class TestNoNativeScrollbarsAnywhere:
    """A scrollbar does two things: says where you are, and lets you drag. In
    this app the first is answered better by what is on screen — the turn rail
    marks questions rather than a proportion of pixels, the film strip shows
    slides, the tree shows files — and the second is a gesture almost nobody
    reaches for when a wheel, a trackpad and the keyboard all do it. What was
    left in every pane was a grey bar down the edge of the content."""

    def test_it_is_one_rule_at_the_root(self):
        """Pane by pane is a rule per scroll container and a missed one every
        time a pane is added. There are 71 of them."""
        assert "* { scrollbar-width: none; }" in CSS
        assert "*::-webkit-scrollbar { width: 0; height: 0; }" in CSS

    def test_scrolling_itself_is_untouched(self):
        """The drawing is hidden, not the behaviour — wheel, trackpad, keyboard
        and the rail all still scroll. `overflow: hidden` would break all four
        and look identical in a screenshot."""
        assert "overflow-y: auto" in re.search(r"#chat-messages \{([^}]*)\}", CSS).group(1)

    def test_the_transcript_has_no_rule_of_its_own(self):
        """It had one first. The root rule supersedes it, and two rules doing
        the same thing is one that gets edited and one that does not."""
        assert "#chat-messages::-webkit-scrollbar" not in CSS


class TestEveryCssTokenIsDefined:
    def test_the_rail_uses_only_real_tokens(self):
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", CSS, re.MULTILINE))
        used = set()
        for selector in (".turn-rail", ".turn-tick", ".turn-tick-label",
                         ".turn-tick-mark"):
            used |= set(re.findall(r"var\((--[a-z0-9-]+)", rule(selector)))
        assert used <= defined, f"undefined CSS tokens: {sorted(used - defined)}"
