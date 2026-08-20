"""The Add-ons page said things were not installed that demonstrably were.

Reported the way these always are: "one of the big things that would happen is
it would say things aren't installed or it wouldn't work, except I know agent
mode works". Screenshot attached, showing browser control offered for install
and a red line under it — from a session in which the agent had just driven a
real browser through a car specification site.

Three separate causes, and the first is the interesting one.

**The check refused to run where it was called.** `/api/components` is an
`async def`, so `status()` runs on the event loop thread, and Playwright's
*sync* API raises there by design: "It looks like you are using Playwright Sync
API inside the asyncio loop." The raise was caught, and the caught exception
became `installed: False`. The page was confidently wrong about the one thing
the user could see working — on the page whose entire job is to say what is
here.

**A failure outlived the thing it failed at.** `_runs` is memory that lives
until the process restarts, so a component that failed to install once kept its
red line for the rest of the session, including after a retry or a restart had
made it work.

**Carrot's own footprint was reported as a stranger.** Running `-m playwright`
against the frozen backend re-launches the app, which fails to bind the port the
first copy holds. What the user saw was "Downloading Chromium did not finish"
over a socket error about an address, which explains nothing to somebody who
pressed a button labelled Install.
"""
import asyncio

import pytest

from carrot import components


@pytest.fixture
def fake_component(monkeypatch):
    """One component whose state this test controls completely."""
    spec = {
        "id": "test-thing",
        "label": "A test thing",
        "unlocks": "Nothing at all.",
        "detail": "",
        "pip": ["nothing"],
        "check": lambda: True,
        "size_hint": "~0 MB",
    }
    monkeypatch.setattr(components, "COMPONENTS", [spec])
    monkeypatch.setattr(components, "_runs", {})
    components.forget_probes()
    return spec


def row(component_id):
    return next(r for r in components.status() if r["id"] == component_id)


class TestTheCheckRunsWhereItCanAnswer:
    """Playwright's sync API raises inside a running event loop, which is
    exactly where the status endpoint calls it from."""

    def test_a_probe_runs_with_no_loop_in_its_thread(self):
        def probe():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return True     # no loop here, which is the whole point
            return False

        assert components._off_the_event_loop(probe) is True

    def test_it_is_still_loop_free_when_called_from_a_loop(self):
        def probe():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return True
            return False

        async def as_the_endpoint_does():
            return components._off_the_event_loop(probe)

        assert asyncio.run(as_the_endpoint_does()) is True

    def test_a_probe_that_raises_is_not_installed_rather_than_an_error(self):
        def broken():
            raise RuntimeError("no driver")

        assert components._off_the_event_loop(broken) is False

    def test_a_probe_that_hangs_does_not_hang_the_page(self, monkeypatch):
        import threading

        monkeypatch.setattr(components, "PROBE_TIMEOUT", 0.2)
        started = threading.Event()

        def hangs():
            started.set()
            threading.Event().wait(30)
            return True

        assert components._off_the_event_loop(hangs) is False
        assert started.is_set()

    def test_the_status_endpoint_answers_from_inside_a_loop(self):
        async def as_the_endpoint_does():
            return components.status()

        rows = asyncio.run(as_the_endpoint_does())
        assert rows and all("installed" in r for r in rows)


class TestTheProbeIsNotRunOnEveryPoll:
    """Answering it starts Playwright's driver process, and the page polls."""

    def test_the_answer_is_cached(self):
        components.forget_probes()
        calls = []

        def probe():
            calls.append(1)
            return True

        assert components._cached("t", probe) is True
        assert components._cached("t", probe) is True
        assert len(calls) == 1

    def test_an_install_forgets_it(self):
        components.forget_probes()
        components._cached("t", lambda: False)
        components.forget_probes()
        assert components._cached("t", lambda: True) is True


class TestAFailureDoesNotOutliveTheThingWorking:

    def test_a_stale_failure_is_dropped_once_it_is_installed(self, fake_component):
        components._runs["test-thing"] = {
            "state": "failed", "message": "Could not install nothing.", "error": "pip said no",
        }
        answer = row("test-thing")
        assert answer["installed"] is True
        assert answer["state"] == "idle"
        assert answer["message"] == ""
        assert answer["error"] == ""

    def test_a_failure_on_something_still_missing_is_kept(self, fake_component, monkeypatch):
        fake_component["check"] = lambda: False
        components.forget_probes()
        components._runs["test-thing"] = {
            "state": "failed", "message": "Could not install nothing.", "error": "pip said no",
        }
        answer = row("test-thing")
        assert answer["installed"] is False
        assert answer["state"] == "failed"
        assert answer["message"] == "Could not install nothing."

    def test_an_install_in_flight_is_still_reported(self, fake_component):
        components._runs["test-thing"] = {"state": "installing", "message": "Installing…"}
        answer = row("test-thing")
        # Only *failure* is suppressed by success. A component being installed
        # again over one that is already there is a thing happening now.
        assert answer["state"] == "installing"


class TestCarrotRecognisesItsOwnFootprint:

    def test_the_port_clash_is_read_as_the_app_relaunching_itself(self):
        tail = ("ERROR:    [Errno 10048] error while attempting to bind on address "
                "('127.0.0.1', 8181): only one usage of each socket address "
                "(protocol/network address/port) is normally permitted")
        assert components._is_relaunch_failure(tail) is True

    def test_an_unrelated_failure_is_not(self):
        # Mislabelling this would send the reader after a Python they do not
        # need, which is worse than the generic message it replaces.
        assert components._is_relaunch_failure("SSL certificate problem: unable to get "
                                               "local issuer certificate") is False
        assert components._is_relaunch_failure("") is False

    def test_the_component_that_can_hit_it_still_looks_for_a_real_python(self):
        # The argv is built when the button is pressed, not at import, because
        # `sys.executable` in the frozen build is carrot-backend itself.
        browser = next(c for c in components.COMPONENTS if c["id"] == "browser")
        argv = browser["post"]["argv"]()
        assert argv[1:] == ["-m", "playwright", "install", "chromium"]
        assert "carrot-backend" not in argv[0].lower()
