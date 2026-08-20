"""Running a whole document — its groups, top to bottom, one at a time.

A group already carried its own route and its own progress bar; what it did not
have was a way to say "and now do all of them". Six groups meant six presses
spread over however long the slowest of them took, and the sixth press happened
when you remembered rather than when the fifth finished.

The decisions this holds, each of which is a decision and not an implementation
detail:

* **In document order, one at a time.** These are jobs on one machine. Firing
  six at once queues them inside Ollama, where nothing in the document can see
  the queue. Order is the document's order because that is the only order the
  document states.
* **A failure does not stop the rest.** Groups are independent routes, not
  steps in a pipeline. The failure stays on its own chip, attached to the thing
  that failed.
* **Stop stops the queue, not the run in flight.** Cancelling a research run is
  that run's own business and its tab has the control.
* **It does not leave the document.** The whole reason a group carries a
  progress bar is so the document can be the place you watch from — flipping
  tabs per group would drag the user through three tabs and back, twenty times.
* **A chat group in a batch sends rather than stages.** Staging is right for
  one document: it becomes a chip and you type the question. In a batch nobody
  is there to type twenty questions, and a Run all that ends with twenty chips
  in the tray has run nothing at all.

The suite reads the source because this is browser code with no browser here.
That is the same bargain the rest of the front-end tests make, and it catches
the class of mistake that actually happens: a control wired to nothing, a state
nothing can leave, a second implementation of something that already exists.
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
def agent_js():
    return (WEB / "js" / "docagent.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html():
    return (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def style_css():
    return (WEB / "css" / "style.css").read_text(encoding="utf-8")


def body_of(source, name):
    """One function's source, from its signature to the next top-level one."""
    start = source.index(f"function {name}(")
    rest = source[start:]
    end = rest.find("\nfunction ", 1)
    tail = rest.find("\nasync function ", 1)
    if tail != -1 and (end == -1 or tail < end):
        end = tail
    return rest if end == -1 else rest[:end]


class TestTheButton:

    def test_run_all_is_wired_to_something(self, index_html, groups_js):
        assert 'id="doc-runall-btn"' in index_html
        assert 'onclick="runAllGroups()"' in index_html
        assert "async function runAllGroups()" in groups_js

    def test_it_is_absent_until_the_document_has_a_group(self, index_html, groups_js):
        assert 'id="doc-runall-btn"' in index_html
        button = re.search(r'<button[^>]*id="doc-runall-btn"[^>]*>', index_html).group(0)
        assert "hidden" in button, "Run all is drawn before there is anything to run"
        assert "button.classList.toggle('hidden', !count)" in groups_js

    def test_the_count_comes_from_the_same_walk_that_draws_the_chips(self, groups_js):
        # Two scans could disagree about what exists: a button offering to run
        # four groups over a document showing three.
        decorations = body_of(groups_js, "groupDecorations")
        assert "syncRunAllButton(index)" in decorations


class TestTheOrderAndTheQueue:

    def test_it_runs_them_in_document_order(self, groups_js):
        run = body_of(groups_js, "runAllGroups")
        # `groupsInDocument` reads the markdown top to bottom; nothing sorts or
        # reverses it, and nothing runs them in parallel.
        assert "groupsInDocument()" in run
        assert ".sort(" not in run and ".reverse()" not in run
        assert "Promise.all" not in run, "a batch that fires them at once is not a queue"

    def test_each_group_is_awaited_before_the_next_is_started(self, groups_js):
        run = body_of(groups_js, "runAllGroups")
        assert "await sendGroup(item.group, item.index, { quiet: true })" in run

    def test_every_group_is_marked_queued_before_the_first_one_starts(self, groups_js):
        run = body_of(groups_js, "runAllGroups")
        marked = run.index("status: 'queued'")
        started = run.index("await sendGroup")
        assert marked < started, "the plan is revealed one group at a time"

    def test_a_queued_group_draws_as_queued(self, groups_js):
        element = body_of(groups_js, "groupRunElement")
        assert "run.status === 'queued'" in element
        # There is nothing to open yet, so it offers nothing to open.
        queued = element[element.index("run.status === 'queued'"):]
        assert "cg-run-view" not in queued[:queued.index("return wrap;")]

    def test_a_group_never_reached_stops_claiming_to_be_queued(self, groups_js):
        run = body_of(groups_js, "runAllGroups")
        assert "=== 'queued') groupRuns.delete(key)" in run


class TestFailure:

    def test_a_failed_send_marks_that_group_and_carries_on(self, groups_js):
        run = body_of(groups_js, "runAllGroups")
        # The catch is inside the loop, so the next group still runs.
        loop = run[run.index("for (const item of runnable)"):]
        assert "catch (_)" in loop
        assert "status: 'failed'" in loop
        assert "break" not in loop.split("catch (_)")[1].split("}")[0]

    def test_a_group_whose_send_failed_offers_nothing_to_open(self, groups_js):
        # It has no run and no id. `openGroupRun` looks the kind up in the
        # rail's openers, finds nothing and returns — so a View there is a
        # button that does nothing, on the one chip whose reader most wants to
        # press something.
        element = body_of(groups_js, "groupRunElement")
        assert "run.status !== 'gone' && run.id" in element

    def test_the_tally_separates_done_from_failed(self, groups_js):
        run = body_of(groups_js, "runAllGroups")
        assert "batchRun.done += 1" in run
        assert "batchRun.failed += 1" in run
        bar = body_of(groups_js, "renderBatchBar")
        assert "done" in bar and "failed" in bar

    def test_only_a_completed_run_counts_as_done(self, groups_js):
        # Both spellings, because research and agent rows do not agree on one
        # and a tally that knows a single spelling reports every success as a
        # failure.
        run = body_of(groups_js, "runAllGroups")
        assert "status === 'complete' || status === 'completed'" in run


class TestStopping:

    def test_stop_ends_the_queue(self, groups_js):
        assert "function stopBatchRun()" in groups_js
        run = body_of(groups_js, "runAllGroups")
        assert "if (batchRun.stop) break;" in run

    def test_stop_does_not_claim_to_cancel_what_is_running(self, groups_js):
        bar = body_of(groups_js, "renderBatchBar")
        assert "nothing further will start" in bar
        assert "carries on" in bar, "the Stop button does not say what it does not do"

    def test_the_bar_offers_a_stop_only_while_there_is_something_to_stop(self, groups_js):
        bar = body_of(groups_js, "renderBatchBar")
        assert "batchRun.stop ? ''" in bar


class TestItStaysInTheDocument:

    def test_a_quiet_dispatch_does_not_change_tabs(self, agent_js):
        dispatch = body_of(agent_js, "dispatchDoc")
        # Every switchTab in the dispatcher is guarded by `quiet`.
        for line in dispatch.splitlines():
            if "switchTab(" in line:
                assert "!quiet" in line, f"unguarded tab switch: {line.strip()}"

    def test_a_chat_group_in_a_batch_sends_rather_than_staging(self, agent_js):
        dispatch = body_of(agent_js, "dispatchDoc")
        assert "if (!quiet && typeof stageDocument === 'function'" in dispatch

    def test_sending_one_group_by_hand_still_goes_and_watches_it(self, groups_js, agent_js):
        # The old behaviour is the default: `quiet` is opt-in, and the chip's
        # own send arrow does not pass it.
        assert "async function dispatchDoc(payload, label, options = {})" in agent_js
        send = body_of(groups_js, "sendGroup")
        assert "quiet: !!options.quiet" in send

    def test_the_progress_strip_is_in_the_document(self, index_html, style_css):
        assert 'id="doc-batch"' in index_html
        # Above the reference bar, inside the editor column — not floating over
        # the chat.
        assert index_html.index('id="doc-batch"') < index_html.index('id="doc-refs"')
        assert ".doc-batch" in style_css


class TestWatchingWhatCannotBeWatched:
    """`/api/activity/run` answers for research and agent runs. A chat group
    has no row anywhere: its answer is a turn in the transcript."""

    def test_only_research_and_agent_are_polled(self, groups_js):
        assert "const WATCHABLE_RUNS = ['research', 'agent'];" in groups_js
        watchable = body_of(groups_js, "watchable")
        assert "WATCHABLE_RUNS.includes(run.kind)" in watchable
        assert "run.id" in watchable

    def test_the_poll_loop_asks_only_about_those(self, groups_js):
        poll = body_of(groups_js, "pollGroupRuns")
        assert "watchable(run)" in poll
        assert "run.status === 'running'" not in poll, \
            "the loop still polls runs that have no row to read"

    def test_a_chat_group_in_a_batch_still_shows_that_it_is_working(self, groups_js):
        send = body_of(groups_js, "sendGroup")
        assert "kind: 'conversation'" in send
        assert "status: 'running'" in send
        assert "status: 'complete'" in send

    def test_the_pending_slot_is_released_even_when_no_run_claims_it(self, groups_js):
        # The bug: chat reports no run id, so the slot stayed set and the
        # *next* group's id was filed against the chat group — one bar showing
        # another group's progress and the real one showing nothing.
        send = body_of(groups_js, "sendGroup")
        assert "finally {" in send
        assert "groupRunPending = null;" in send

    def test_a_finished_run_is_read_at_once_rather_than_at_the_next_poll(self, groups_js):
        assert "async function settleGroupRun(key)" in groups_js
        run = body_of(groups_js, "runAllGroups")
        assert "await settleGroupRun(key)" in run

    def test_asking_after_a_run_exists_once(self, groups_js):
        # The poll and the batch's own settle both need it, and two copies is
        # how one of them ends up knowing something about the answer — a 404
        # means gone — that the other does not.
        settle = body_of(groups_js, "settleGroupRun")
        assert "/api/activity/run" in settle
        assert groups_js.count("api('/api/activity/run") == 1, \
            "progress is requested from more than one place"
        assert "await settleGroupRun(key)" in body_of(groups_js, "pollGroupRuns")
