"""When a message was said, and what it cost.

Both were already known and neither was shown. The timestamp was a column on
every row; the rate was read from Ollama's own counters into a throughput meter
that only the dashboard looked at. So the one place somebody wonders why a turn
was slow — under the turn — was the one place with nothing on it.
"""
import time
from pathlib import Path

import pytest

from carrot import sysmon
from carrot.app import _turn_metrics

WEB = Path(__file__).resolve().parents[1] / "carrot" / "web"
JS = (WEB / "js" / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "css" / "style.css").read_text(encoding="utf-8")


def block(name):
    start = JS.index(f"function {name}")
    return JS[start:JS.index("\n}", start)]


@pytest.fixture(autouse=True)
def _empty_meter():
    sysmon.throughput.clear()
    yield
    sysmon.throughput.clear()


class TestTheRateBelongsToItsOwnTurn:
    def test_a_turn_reports_what_it_measured(self):
        started = time.perf_counter()
        sysmon.throughput.record("phi4:14b", eval_count=300,
                                 eval_duration_ns=6_000_000_000)
        metrics = _turn_metrics(started)["metrics"]
        assert metrics["tps"] == 50.0
        assert metrics["tokens"] == 300
        assert metrics["model"] == "phi4:14b"

    def test_a_turn_where_the_model_never_ran_reports_nothing(self):
        """A hosted model, a cached answer, a reply that was entirely tool
        output. Borrowing the previous turn's rate would be a number that looks
        measured and is not — worse than a blank line, because it is believed.
        """
        sysmon.throughput.record("phi4:14b", eval_count=300,
                                 eval_duration_ns=6_000_000_000)
        later = time.perf_counter()
        assert _turn_metrics(later) == {}

    def test_it_is_not_fooled_by_a_coarse_clock(self):
        """`time.time()` and `time.monotonic()` both move in ~15ms steps on
        Windows, so a turn that began and ended inside one tick compared equal
        to its own start — losing its sample, or taking the previous turn's.
        The stamp is a perf_counter for that reason."""
        started = time.perf_counter()
        sysmon.throughput.record("m", eval_count=99, eval_duration_ns=1_000_000_000)
        assert _turn_metrics(started) != {}
        sample = sysmon.throughput.since(started)
        assert "at_mono" in sample

    def test_prompt_processing_is_reported_separately(self):
        """A long context is slow to ingest even when generation is fast, and
        one number hides which of the two somebody waited on."""
        started = time.perf_counter()
        sysmon.throughput.record("m", eval_count=300, eval_duration_ns=6_000_000_000,
                                 prompt_eval_count=1200,
                                 prompt_eval_duration_ns=1_000_000_000)
        metrics = _turn_metrics(started)["metrics"]
        assert metrics["prompt_tokens"] == 1200
        assert metrics["prompt_tps"] == 1200.0


class TestDeletingOneMessage:
    def test_it_needs_the_right_conversation(self, client, isolated_db):
        """A message id is a bare autoincrement integer. Scoped by id alone, a
        stale or mistyped one would delete out of somebody else's thread."""
        from carrot import conversation as conv

        one = conv.create_conversation("one")
        two = conv.create_conversation("two")
        message = conv.add_message(one["id"], "user", "in one")

        wrong = client.delete(f"/api/conversations/{two['id']}/messages/{message['id']}")
        assert wrong.status_code == 404
        assert conv.get_conversation(one["id"])["messages"], "it was deleted anyway"

        right = client.delete(f"/api/conversations/{one['id']}/messages/{message['id']}")
        assert right.status_code == 200
        assert not conv.get_conversation(one["id"])["messages"]

    def test_deleting_a_message_that_is_gone_is_a_404(self, client, isolated_db):
        from carrot import conversation as conv

        made = conv.create_conversation("one")
        assert client.delete(
            f"/api/conversations/{made['id']}/messages/99999").status_code == 404


class TestTheLineUnderTheMessage:
    def test_it_is_built_and_styled(self):
        assert "function renderMessageMeta" in JS
        assert "renderMessageMeta(div)" in block("attachMessageActions")
        for cls in (".msg-meta", ".msg-meta-tps"):
            assert cls in CSS, f"{cls} is built but never styled"

    def test_nothing_is_shown_when_there_is_nothing_to_show(self):
        """An empty strip under every message would be furniture."""
        body = block("renderMessageMeta")
        assert "if (!bits.length) return;" in body

    def test_a_rate_is_only_shown_when_one_was_measured(self):
        body = block("renderMessageMeta")
        assert "metrics && metrics.tps" in body

    def test_the_detail_is_in_the_tooltip_rather_than_the_line(self):
        """The model, the seconds, the prompt rate — worth having, not worth
        putting under every answer."""
        body = block("renderMessageMeta")
        assert "title=" in body
        assert "prompt_tps" in body

    def test_reopening_a_chat_keeps_it(self):
        """The timestamp and the metrics were already on the row from the
        server; replaying history simply was not reading them."""
        assert "at: m.timestamp" in JS
        assert "(m.metadata || {}).metrics" in JS

    def test_delete_is_offered_and_confirmed(self):
        assert "function deleteMessage" in JS
        assert "confirm(" in block("deleteMessage")

    def test_delete_asks_the_scoped_endpoint(self):
        body = block("deleteMessage")
        assert "currentConversationId" in body
        assert "/messages/" in body
