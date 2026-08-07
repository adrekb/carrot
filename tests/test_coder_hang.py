"""A coding turn that stops after an approval and never says it is done.

Reported with a screenshot: "Edit magnetic_field_sim/ui.py (1 change(s)) —
allowed", and then nothing. No further output, no completion, the Stop button
still showing. The edit was approved, so the gate did its job; what did not
happen is the turn carrying on afterwards.

These drive the real loop with a real approval — blocking on a worker thread,
answered from another — because every part of this works in isolation and the
question is whether they work together.
"""
import threading

import pytest

from carrot import agent_tools, app as A


def tool_call(name, **args):
    return [{"id": name, "function": {"name": name, "arguments": args}}]


def answer_approvals(decision="allow", tries=200):
    """Answer prompts as they appear, from another thread, until told to stop."""
    stop = threading.Event()
    answered = []

    def loop():
        for _ in range(tries):
            if stop.is_set():
                return
            for pending in agent_tools.pending_approvals():
                agent_tools.resolve_approval(pending["id"], decision)
                answered.append(pending["tool"])
            threading.Event().wait(0.02)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return stop, answered, thread


def run_turn(script, isolated_db, timeout=25):
    """Run a coder turn to completion on a worker thread, or fail loudly.

    The turn is driven on a thread with a join timeout so a hang is a test
    failure rather than a hung test run — which is the whole point here.
    """
    from unittest.mock import patch

    remaining = iter(script)

    def fake_stream(resolved, messages, tools=None):
        try:
            content, calls = next(remaining)
        except StopIteration:
            content, calls = ("All done.", [])
        if content:
            yield {"type": "text", "text": content}
        if calls:
            yield {"type": "tool_calls", "calls": calls}

    class Route:
        def as_dict(self):
            return {}

    events = []
    error = {}

    def drive():
        try:
            history = [{"role": "user", "content": "add a retry loop to client.py"}]
            events.extend(A._agentic_chat_events(history, Route(), None, None, A.SEARCH_OFF))
        except Exception as exc:          # pragma: no cover - reported as a failure
            error["exc"] = exc

    stop, answered, answerer = answer_approvals()
    with patch.object(A.router_mod, "stream_events", fake_stream), \
            patch.object(A, "_available_tools", lambda m: []):
        thread = threading.Thread(target=drive, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
    stop.set()
    answerer.join(timeout=2)

    assert not thread.is_alive(), (
        "the turn never finished — this is the reported hang, and it is what "
        "leaves the panel with a spinner and no completion"
    )
    if error:
        raise error["exc"]
    return {
        "events": events,
        "final": next((e["_final_text"] for e in events if "_final_text" in e), None),
        "approved": answered,
        "tools": [e["tool"]["name"] for e in events if "tool" in e],
    }


@pytest.fixture(autouse=True)
def _clean_session():
    agent_tools.reset_session_approvals()
    yield
    agent_tools.reset_session_approvals()


class TestATurnThatNeedsApprovalStillFinishes:
    def test_one_approved_write_then_an_answer(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        result = run_turn([
            ("", tool_call("carrot__write_file", path="client.py", content="retry()")),
            ("Added the retry loop.", []),
        ], isolated_db)
        assert result["final"] == "Added the retry loop."
        assert "carrot__write_file" in result["tools"]

    def test_the_turn_ends_even_with_several_approvals(self, isolated_db, tmp_path, monkeypatch):
        # The reported turn was one edit, but a coding turn routinely writes
        # three or four files, and each one blocks the loop again.
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        script = []
        for n in range(4):
            script.append(("", tool_call("carrot__write_file",
                                         path=f"f{n}.py", content=str(n))))
        script.append(("Wrote four files.", []))
        result = run_turn(script, isolated_db)
        assert result["final"] == "Wrote four files."
        assert len(result["approved"]) >= 1

    def test_a_denied_write_still_produces_an_answer(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        from unittest.mock import patch

        remaining = iter([
            ("", tool_call("carrot__write_file", path="client.py", content="x")),
            ("I did not change anything.", []),
        ])

        def fake_stream(resolved, messages, tools=None):
            try:
                content, calls = next(remaining)
            except StopIteration:
                content, calls = ("Done.", [])
            if content:
                yield {"type": "text", "text": content}
            if calls:
                yield {"type": "tool_calls", "calls": calls}

        class Route:
            def as_dict(self):
                return {}

        events, done = [], threading.Event()
        stop, _, answerer = answer_approvals(decision="deny")

        def drive():
            try:
                events.extend(A._agentic_chat_events(
                    [{"role": "user", "content": "write it"}], Route(), None, None,
                    A.SEARCH_OFF))
            finally:
                done.set()

        with patch.object(A.router_mod, "stream_events", fake_stream), \
                patch.object(A, "_available_tools", lambda m: []):
            thread = threading.Thread(target=drive, daemon=True)
            thread.start()
            thread.join(timeout=25)
        stop.set()
        answerer.join(timeout=2)
        assert done.is_set(), "a denied approval must not hang the turn either"
        assert next(e["_final_text"] for e in events if "_final_text" in e)


class TestTheStreamAlwaysCloses:
    """Whatever happens inside, the panel needs the stream to end.

    The Code tab clears its spinner when the response body ends, not on any
    particular event — so a turn that stops producing output without closing
    leaves it running forever.
    """

    def test_done_is_the_last_thing_sent(self, client, tmp_path, monkeypatch):
        from carrot import agent_tools as tools

        monkeypatch.setattr(tools, "workspace_root", lambda: str(tmp_path))
        with client.stream("POST", "/api/chat/stream", json={
            "message": "hello", "search_mode": "off", "coder": True,
        }) as response:
            body = "".join(response.iter_text())
        assert '"done": true' in body or '"done":true' in body

    def test_an_exploding_turn_still_closes(self, client, monkeypatch):
        # The generator has already sent a 200 by the time it runs, so an
        # exception here is not an error response — it is a socket that shuts
        # without explanation, which is exactly what a hang looks like.
        def boom(*a, **k):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(A, "_agentic_chat_events", boom)
        with client.stream("POST", "/api/chat/stream", json={
            "message": "hello", "search_mode": "off",
        }) as response:
            body = "".join(response.iter_text())
        assert "provider exploded" in body
        assert '"done": true' in body or '"done":true' in body


class TestABlockedTurnSaysItIsBlocked:
    """The reported symptom, and the thing that made it unreadable.

    A turn waiting on approval emitted the prompt and then went completely
    silent — no output, no heartbeat, no end. From the panel a turn being
    patient and a turn that has died are the same picture, and people report
    the second one.
    """

    def wait_for(self, decision_after=None, timeout=3, heartbeat=1):
        """Run one approval wait, capturing what it emitted."""
        from unittest.mock import patch

        emitted = []
        request = agent_tools.ApprovalRequest("write_file", {}, "Write demo.py", "low")

        if decision_after is not None:
            def answer():
                threading.Event().wait(decision_after)
                request.decision = "allow"
                request.event.set()
            threading.Thread(target=answer, daemon=True).start()

        with patch.object(agent_tools, "APPROVAL_HEARTBEAT_SECONDS", heartbeat):
            answered = agent_tools._wait_saying_so(request, timeout, emitted.append)
        return answered, [e for e in emitted if "approval_waiting" in e]

    def test_it_keeps_saying_so_while_it_waits(self):
        answered, beats = self.wait_for(timeout=3, heartbeat=1)
        assert answered is False
        assert len(beats) >= 2, "a silent wait is indistinguishable from a dead turn"

    def test_the_heartbeat_says_what_is_being_waited_on(self):
        _, beats = self.wait_for(timeout=2, heartbeat=1)
        first = beats[0]["approval_waiting"]
        assert first["tool"] == "write_file"
        assert first["summary"] == "Write demo.py"
        assert first["seconds"] >= 1
        assert first["seconds_left"] >= 0

    def test_an_answer_stops_it_immediately(self):
        answered, beats = self.wait_for(decision_after=0.1, timeout=5, heartbeat=1)
        assert answered is True
        assert beats == [], "answering promptly should not produce a wait notice"

    def test_it_still_returns_false_on_a_real_timeout(self):
        answered, _ = self.wait_for(timeout=2, heartbeat=1)
        assert answered is False

    def test_it_does_not_overshoot_the_timeout(self):
        import time

        started = time.monotonic()
        self.wait_for(timeout=2, heartbeat=5)   # heartbeat longer than the wait
        assert time.monotonic() - started < 4

    def test_no_emit_channel_is_survivable(self):
        # Research and the blocking API call it without a stream attached.
        request = agent_tools.ApprovalRequest("write_file", {}, "s", "low")
        request.event.set()
        assert agent_tools._wait_saying_so(request, 1, None) is True


class TestThePanelsShowIt:
    def read_js(self, name):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        return (root / "carrot" / "web" / "js" / name).read_text(encoding="utf-8")

    def test_every_stream_handles_the_heartbeat(self):
        # Three panels consume approvals — chat, the agent run, and the Code
        # tab — and the Code tab is the one that gets left running.
        assert "payload.approval_waiting" in self.read_js("app.js")
        assert "event.approval_waiting" in self.read_js("agents.js")
        assert "payload.approval_waiting" in self.read_js("features.js")

    def test_an_answered_card_stops_updating(self):
        assert "if (!box || box.dataset.answered) return;" in self.read_js("features.js")


class TestALeavingClientDoesNotStrandTheTurn:
    """A turn blocks on approval for up to half an hour.

    If the browser goes away in the meantime — tab closed, window reloaded, app
    quit — that wait used to carry on regardless: a held thread, a pending
    question in a list nobody is reading, and eventually a timeout reported as
    the user failing to respond. Measured at twenty-seven minutes, twenty-six
    of them with nothing on the other end. The next run then found the stale
    prompt still sitting there.
    """

    def test_abandoning_releases_the_waiter(self):
        request = agent_tools.ApprovalRequest("write_file", {}, "Write x.py", "low")
        with agent_tools._pending_lock:
            agent_tools._pending[request.id] = request

        released = threading.Event()

        def wait():
            agent_tools._wait_saying_so(request, 30, None)
            released.set()

        threading.Thread(target=wait, daemon=True).start()
        threading.Event().wait(0.1)
        assert agent_tools.abandon(request.id) is True
        assert released.wait(timeout=5), "the waiting turn was not let go"

    def test_it_is_not_recorded_as_a_denial(self):
        # The action was never judged. Telling the model the user said no would
        # have it apologise for a decision nobody made, and carry that
        # misreading into the rest of the conversation.
        request = agent_tools.ApprovalRequest("write_file", {}, "Write x.py", "low")
        with agent_tools._pending_lock:
            agent_tools._pending[request.id] = request
        agent_tools.abandon(request.id)
        assert request.decision == agent_tools.DECISION_ABANDONED
        assert agent_tools.ABANDONED_REASON != agent_tools.DENIED_REASON

    def test_the_reason_reaches_the_model_as_its_own_thing(self):
        from unittest.mock import patch

        request = agent_tools.ApprovalRequest("write_file", {}, "Write x.py", "low")

        def fake_wait(req, timeout, emit):
            req.decision = agent_tools.DECISION_ABANDONED
            return True

        with patch.object(agent_tools, "ApprovalRequest", lambda *a, **k: request), \
                patch.object(agent_tools, "_wait_saying_so", fake_wait):
            granted, reason, remembered = agent_tools.request_approval(
                "write_file", {}, "Write x.py", "low", emit=lambda e: None)
        assert granted is False
        assert reason == agent_tools.ABANDONED_REASON
        assert remembered is False

    def test_abandoning_something_unknown_says_so(self):
        assert agent_tools.abandon("no-such-id") is False

    def test_it_leaves_the_pending_list_clean(self):
        request = agent_tools.ApprovalRequest("write_file", {}, "Write x.py", "low")
        with agent_tools._pending_lock:
            agent_tools._pending[request.id] = request
        agent_tools.abandon(request.id)
        assert all(p["id"] != request.id for p in agent_tools.pending_approvals())

    def test_an_answered_prompt_cannot_then_be_abandoned(self):
        # The decision is made; a late disconnect must not rewrite it.
        request = agent_tools.ApprovalRequest("write_file", {}, "Write x.py", "low")
        with agent_tools._pending_lock:
            agent_tools._pending[request.id] = request
        agent_tools.resolve_approval(request.id, "allow")
        assert agent_tools.abandon(request.id) is False
        assert request.decision == "allow"


class TestTheStreamCleansUpAfterItself:
    def source(self):
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "carrot" / "app.py").read_text(
            encoding="utf-8")

    def test_outstanding_prompts_are_tracked_off_the_stream(self):
        # No plumbing through every layer between the endpoint and the gate:
        # the ids are already in the stream, and the stream is the thing that
        # knows whether anyone is still receiving it.
        source = self.source()
        assert 'outstanding.add(event["approval_request"]["id"])' in source
        assert 'outstanding.discard(event["approval_resolved"]["id"])' in source

    def test_they_are_released_however_the_generator_ends(self):
        source = self.source()
        block = source[source.index("    def stream():\n        \"\"\"The body, plus"):]
        assert "finally:" in block[:block.index("return StreamingResponse")]
        assert "agent_mod.abandon(approval)" in block[:block.index("return StreamingResponse")]

    def test_a_normal_finish_costs_nothing(self, client, tmp_path, monkeypatch):
        from carrot import agent_tools as tools

        monkeypatch.setattr(tools, "workspace_root", lambda: str(tmp_path))
        with client.stream("POST", "/api/chat/stream", json={
            "message": "hello", "search_mode": "off",
        }) as response:
            body = "".join(response.iter_text())
        assert '"done": true' in body or '"done":true' in body
        assert agent_tools.pending_approvals() == []

    def test_a_decision_in_flight_is_not_overwritten(self):
        # The narrow window that matters: `resolve_approval` records the answer
        # and leaves the request in the list until the waiting turn wakes and
        # removes it. Click Allow, close the tab, and a disconnect arriving in
        # between must not rewrite what the user already said.
        request = agent_tools.ApprovalRequest("write_file", {}, "Write x.py", "low")
        with agent_tools._pending_lock:
            agent_tools._pending[request.id] = request
        agent_tools.resolve_approval(request.id, "deny")
        assert agent_tools.abandon(request.id) is False
        assert request.decision == "deny"
