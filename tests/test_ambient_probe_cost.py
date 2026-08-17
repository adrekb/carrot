"""Asking what is switched on must not wait for the graphics card.

`GET /api/ambient` answers two questions at once: what the rules are, and what
they say about this moment. The second half reads memory, VRAM and whether
Ollama has a model resident — and the Ollama probe waited a full two-second
timeout every call on a machine where Ollama is not running, which is the
ordinary state before somebody sets it up.

That call sits inside an `async def`, so the two seconds were spent on the event
loop with every other request in the process queued behind it. Measured on a
cold process the whole endpoint took 2.06s; the privacy panel, which only wants
to know which switches are on, was paying all of it and reporting "could not
check" when anything gave up waiting.
"""
import time

import pytest

from carrot import ambient


@pytest.fixture(autouse=True)
def _clear_the_probe_cache():
    ambient._busy_cache["at"] = 0.0
    ambient._busy_cache["value"] = False
    yield
    ambient._busy_cache["at"] = 0.0
    ambient._busy_cache["value"] = False


class TestTheOllamaProbe:
    def test_it_fails_fast(self):
        """A reachable Ollama answers /api/ps in single-digit milliseconds, so
        a long timeout only ever buys a longer wait for "not there"."""
        assert ambient._BUSY_TIMEOUT <= 0.5

    def test_a_burst_of_callers_pays_once(self, monkeypatch):
        """The settings panel, the capture loop and the privacy panel all ask
        within a second of each other."""
        calls = []

        def fake_get(url, timeout=None):
            calls.append(url)
            raise OSError("connection refused")

        import requests

        monkeypatch.setattr(requests, "get", fake_get)
        for _ in range(5):
            ambient._model_busy()
        assert len(calls) == 1, f"probed {len(calls)} times for five callers"

    def test_an_unreachable_ollama_is_not_read_as_busy(self, monkeypatch):
        """Erring the other way would mean capture never runs on a machine
        with no Ollama at all."""
        import requests

        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        assert ambient._model_busy() is False

    def test_the_cache_expires(self, monkeypatch):
        """A stale "busy" that never cleared would stop capture permanently
        after one unlucky probe."""
        assert ambient._BUSY_CACHE_SECONDS <= 10

    def test_probing_is_quick_even_with_nothing_listening(self):
        """The end-to-end property, in the state that actually caused the bug:
        no Ollama, so every probe hits its timeout."""
        start = time.perf_counter()
        for _ in range(3):
            ambient.probe_resources()
        assert time.perf_counter() - start < 2.0


class TestNothingBlocksTheLoop:
    """Each of these probes the machine, and each is reached from an `async def`
    — so each has to be handed to a thread or it is spent on the event loop."""

    @pytest.mark.parametrize("handler", ["state", "capture_status", "check", "capture_now"])
    def test_the_probing_endpoints_use_a_thread(self, handler):
        import inspect

        from carrot import ambient_api

        source = inspect.getsource(getattr(ambient_api, handler))
        assert "to_thread" in source, f"{handler} probes the machine on the event loop"


class TestTheCheapQuestion:
    def test_there_is_a_way_to_read_the_switches_alone(self, client, isolated_db):
        """Anything that only wants to know what is on should not have to wait
        for nvidia-smi to start."""
        body = client.get("/api/ambient/policy").json()
        assert "policy" in body
        assert "enabled" in body["policy"]
        assert "agent_aware" in body["policy"]

    def test_it_probes_nothing(self, client, isolated_db, monkeypatch):
        """If this ever starts calling probe_resources it stops being the cheap
        question and the panel is slow again, silently."""
        def explode(*a, **k):
            raise AssertionError("the policy endpoint probed the machine")

        monkeypatch.setattr(ambient, "probe_resources", explode)
        monkeypatch.setattr(ambient, "_model_busy", explode)
        assert client.get("/api/ambient/policy").status_code == 200

    def test_the_privacy_panel_asks_the_cheap_one(self):
        from pathlib import Path

        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
              / "app.js").read_text(encoding="utf-8")
        start = js.index("function fillPrivacyPanel")
        body = js[start:js.index("\n}", start)]
        assert "/api/ambient/policy" in body
        # And not the expensive one, which is what it used to ask.
        assert "'/api/ambient'" not in body
