"""What the agent did, shown as cards instead of as a log.

Every tool call rendered as two lines of trace — `→ edit_file(path=…,
edits=<<<<<<< SEARCH…)` and `← ok`. That is a transcript of the machinery, and
it has the two properties you least want in the thing you read while deciding
whether to trust a change: the filename is buried mid-line and the edit itself
is truncated at sixty characters. Six edits in a row were six identical lines.

The bug that these mostly exist to keep dead is subtler than any of that. The
answer streams into `.agent-body` by replacing its innerHTML on every chunk,
so a card appended inside it survives until the next token — the edits would
have disappeared one by one while the model wrote its summary of them.
"""
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "carrot" / "web"
FEATURES = (WEB / "js" / "features.js").read_text(encoding="utf-8")
CSS = (WEB / "css" / "style.css").read_text(encoding="utf-8")


def _function(name):
    """The source of one top-level function, for asserting about its body."""
    start = FEATURES.index(f"function {name}(")
    rest = FEATURES[start:]
    end = rest.index("\n}\n")
    return rest[:end]


class TestCardsSurviveTheAnswer:
    """The one that would have been found in use rather than in review."""

    @pytest.mark.parametrize("builder", ["agentToolCard", "agentServerCard"])
    def test_a_card_is_a_sibling_of_the_answer_not_a_child(self, builder):
        body = _function(builder)
        assert "querySelector('.agent-body')" not in body, (
            f"{builder} puts its card inside the element the stream overwrites")
        assert "wrap.appendChild" in body

    def test_the_answer_really_does_replace_that_element(self):
        """If this ever stops being true the rule above can be relaxed — and
        if it silently stopped being true, the rule above is still correct."""
        assert "body.innerHTML = mdToHtml(stripQuestions(answer))" in FEATURES


class TestWhatGetsACard:
    def test_only_the_calls_whose_result_you_would_check(self):
        """A panel where everything is a card is a panel where nothing stands
        out, so a search stays a trace line."""
        block = FEATURES[FEATURES.index("const CARD_TOOLS"):]
        block = block[:block.index("]);")]
        for tool in ("run_command", "edit_file", "write_file", "start_server"):
            assert tool in block
        for not_a_card in ("web_search", "read_file", "list_dir"):
            assert not_a_card not in block

    def test_anything_else_still_gets_its_trace_line(self):
        assert "agentTrace(wrap, `→ ${payload.tool.name}" in FEATURES

    def test_a_result_lands_on_the_card_that_asked_for_it(self):
        """No ids are sent and none are needed: the backend runs one tool at a
        time, so the card waiting for a result is the last one made."""
        assert "pendingCard = agentToolCard(wrap, payload.tool)" in FEATURES
        assert "agentToolCardResult(pendingCard" in FEATURES
        # And a non-card tool must clear it, or its result would be written
        # onto whichever card happened to be waiting.
        assert re.search(r"} else \{\s*\n\s*pendingCard = null;", FEATURES)


class TestTheDiff:
    def test_the_block_markers_do_not_reach_the_screen(self):
        """`<<<<<<< SEARCH` is protocol. Showing it is showing the user the
        envelope instead of the letter."""
        body = _function("parseEditBlocks")
        assert "SEARCH" in body and "REPLACE" in body   # it strips them
        assert "kind: 'del'" in body and "kind: 'add'" in body

    def test_an_overwrite_is_not_rendered_as_a_diff_against_nothing(self):
        """The old content is not in the event. A diff against a file nobody
        sent would be a guess wearing the clothes of a fact."""
        body = _function("diffLinesFor")
        assert "'add'" in body[body.index("write_file"):]

    def test_a_huge_diff_is_capped(self):
        """A generated file is four thousand green lines between you and the
        next thing you need to read."""
        assert "CARD_MAX_DIFF_LINES" in FEATURES
        assert "more lines" in FEATURES

    @pytest.mark.parametrize("cls", [
        ".tool-card", ".tool-head", ".tool-title", ".tool-plus", ".tool-minus",
        ".tool-state", ".tool-diff", ".tool-output", ".dl.add", ".dl.del",
    ])
    def test_every_class_it_builds_is_styled(self, cls):
        assert cls in CSS, f"{cls} is built by features.js and never styled"

    def test_added_and_removed_lines_are_told_apart_by_more_than_a_character(self):
        """A leading + or - is indistinguishable from the code when the code
        is itself a diff, which is what an edit to this project looks like."""
        block = CSS[CSS.index(".dl.add {"):]
        assert "background" in block[:block.index("}")]


class TestTheResult:
    def test_a_failed_command_is_marked_as_failed(self):
        body = _function("agentToolCardResult")
        assert "exit" in body
        assert "bad" in body and "good" in body

    def test_the_status_is_not_repeated_inside_the_output(self):
        """`[ok]` is already on the card as a green marker; printing it again
        as the first line of the output is noise."""
        body = _function("agentToolCardResult")
        assert "replace(/^\\[(ok|exit" in body
