"""What the server says it read, against what we guessed it would.

Everything in the chat loop *estimates* the prompt — four characters to the
token — and compares that estimate to a window it believes it has. Both halves
were wrong at once (see docs/postmortem/0001): the estimator is approximate by
construction, and the window was read off the model's ceiling rather than the
`num_ctx` the request runs in. Two wrong numbers that agree with each other
raise no alarm, so Ollama quietly dropped the front of the prompt while the
meter reported plenty of room.

`prompt_eval_count` comes back on the final frame and is measured by the thing
doing the truncating. It is the only number in the turn that is not a guess.

Borrowed from DeepSeek Harness, which compacts on provider-confirmed overflow as
well as on estimated pressure. Their idea; this is Ollama's version of it.
"""
import inspect

import pytest

from carrot import app, ollama_client


class TestTheClientReportsWhatItWasCharged:

    def source(self):
        return inspect.getsource(ollama_client.OllamaClient.chat_stream_events)

    def test_the_final_frame_is_read_for_the_prompt_count(self):
        body = self.source()
        assert 'data.get("prompt_eval_count")' in body

    def test_it_is_reported_with_the_window_it_ran_in(self):
        # Without the window the number means nothing — 31,000 tokens is
        # comfortable in 262,144 and truncated in 32,768.
        body = self.source()
        assert '"type": "usage"' in body
        assert '"window": self.context_length(model)' in body

    def test_a_frame_without_the_count_reports_nothing(self):
        # Not every provider sends it, and inventing a zero would read as a
        # prompt that cost nothing.
        body = self.source()
        assert "if sent:" in body


class TestTheLoopActsOnTheMeasurement:

    def source(self):
        return inspect.getsource(app._agentic_chat_events)

    def test_the_event_has_its_own_branch(self):
        # The `else` arm reads `event["text"]`, which a usage event does not
        # have. Falling through to it would be a KeyError on the last frame of
        # every local turn.
        body = self.source()
        assert 'elif event["type"] == "usage":' in body

    def test_a_full_window_is_treated_as_truncated(self):
        body = self.source()
        assert "measured_prompt >= ceiling * OVERFLOW_FRACTION" in body

    def test_truncation_is_said_out_loud(self):
        # The user's copy of this failure was an answer that had lost its
        # instructions and did not know it.
        body = self.source()
        assert "the prompt was truncated" in body

    def test_it_forces_the_prune_rather_than_waiting_for_the_estimate(self):
        # The estimate is what failed to notice in the first place, so a
        # confirmed truncation must not have to convince it.
        body = self.source()
        assert "or truncated) and round_index:" in body

    def test_the_measurement_calibrates_the_estimate(self):
        body = self.source()
        assert "estimate_scale = max(0.5, min(2.0, measured_prompt / estimated))" in body
        assert "used = int(estimated * estimate_scale)" in body

    def test_the_meter_says_which_number_it_is_showing(self):
        # A measured figure and an estimated one are different kinds of fact.
        body = self.source()
        assert '"measured": True' in body


class TestTheCalibrationIsBounded:
    """A ratio taken from a short prompt swings wildly, and an unbounded one
    would let a single odd round send the meter somewhere useless."""

    def test_a_short_prompt_does_not_recalibrate(self):
        body = inspect.getsource(app._agentic_chat_events)
        assert "if estimated >= CALIBRATION_MIN_TOKENS:" in body

    def test_the_scale_is_clamped_both_ways(self):
        assert app.OVERFLOW_FRACTION < 1.0
        assert app.CALIBRATION_MIN_TOKENS > 0
        body = inspect.getsource(app._agentic_chat_events)
        assert "max(0.5, min(2.0," in body

    @pytest.mark.parametrize("measured,estimated,expected", [
        (30000, 30000, 1.0),      # a perfect estimate changes nothing
        (45000, 30000, 1.5),      # denser than four characters a token
        (15000, 30000, 0.5),      # thinner
        (300000, 30000, 2.0),     # clamped rather than believed
        (300, 30000, 0.5),        # clamped the other way
    ])
    def test_the_arithmetic(self, measured, estimated, expected):
        assert max(0.5, min(2.0, measured / estimated)) == expected


class TestThePostmortemIndexIsHonest:
    """An index that names files which are not there is worse than no index."""

    def entries(self):
        from pathlib import Path

        return Path(__file__).resolve().parents[1] / "docs" / "postmortem"

    def test_every_linked_entry_exists(self):
        import re

        readme = (self.entries() / "README.md").read_text(encoding="utf-8")
        linked = re.findall(r"\]\((\d{4}-[^)]+\.md)\)", readme)
        assert linked, "the index links to nothing"
        for name in linked:
            assert (self.entries() / name).exists(), name

    def test_every_entry_is_linked(self):
        readme = (self.entries() / "README.md").read_text(encoding="utf-8")
        for path in self.entries().glob("[0-9]*.md"):
            assert path.name in readme, f"{path.name} is not in the index"

    def test_each_one_names_the_test_that_holds_it(self):
        # The fix is only finished when something fails if it comes back.
        for path in self.entries().glob("[0-9]*.md"):
            text = path.read_text(encoding="utf-8")
            assert "## Held by" in text, path.name
            assert "test" in text.split("## Held by")[1], path.name
