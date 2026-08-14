"""Shared pytest fixtures for the Carrot test suite.

Provides an isolated SQLite database (via monkeypatched module paths) and a
FastAPI ``TestClient`` with a mocked Ollama client so tests never require a
running Ollama server.
"""
import os

# Before carrot is imported, because the daemons read this when they are
# asked to start and a fixture only covers the tests that request it. Every
# background poller in the app refuses under this flag: they tick against
# whatever database path is current, and in a suite that gives each test its
# own database, that means writing into directories other tests have already
# torn down.
os.environ.setdefault("CARROT_NO_BACKGROUND", "1")

import pytest  # noqa: E402

from carrot import database, config  # noqa: E402


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point the database and config modules at a temporary SQLite file.

    In its own subdirectory, not `tmp_path` itself. The checkpoint tests do
    `git init` in `tmp_path`, so with the database sitting beside them the
    repo under test contained the harness's own SQLite file — `git add -A`
    captured `carrot.db` into every checkpoint, and restoring one ran
    `checkout-index -f` over a database the suite still had open. On Windows
    that cannot unlink, so the restore raised `unable to unlink old
    'carrot.db'` and the test failed. Whether it failed depended on whether a
    connection happened to be open at that moment, which is what made it look
    like flakiness rather than a fixture putting two things in one place.
    """
    from carrot import security

    data = tmp_path / "_carrot"
    data.mkdir(exist_ok=True)
    db_path = str(data / "carrot.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DBCORE_DIR", str(data))
    monkeypatch.setattr(config, "CARROT_DIR", str(data))
    # Keep the session token out of the real data directory too — including
    # the path kept for reading a token written by an older version, which
    # otherwise lets a real one leak into the tests.
    monkeypatch.setattr(security, "CONFIG_DIR", str(data / "config"))
    monkeypatch.setattr(security, "TOKEN_PATH", str(data / "config" / "session.json"))
    monkeypatch.setattr(security, "LEGACY_TOKEN_PATH", str(data / "config" / "legacy.json"))
    monkeypatch.setattr(security, "_token", None)
    database.init_db()
    return db_path


class FakeOllamaClient:
    """A stand-in for OllamaClient that needs no running server."""

    def __init__(self, *args, **kwargs):
        self.default_model = "gemma4:e4b"

    def is_available(self):
        return True

    def list_models(self):
        return [{"name": "gemma4:e4b", "size": 4_200_000_000,
                 "modified_at": "2026-07-01T00:00:00Z", "parameter_size": "4B"}]

    def chat(self, messages, model=None, stream=False):
        if stream:
            return iter(["Hello", " from", " Carrot"])
        return "Hello from Carrot"

    def chat_stream_events(self, messages, model=None, tools=None):
        yield {"type": "thinking", "text": "Considering the question."}
        for chunk in ["Hello", " from", " Carrot"]:
            yield {"type": "content", "text": chunk}

    def structured_chat(self, messages, model=None, response_format=None):
        """Return a JSON string; used by deep-research intent derivation."""
        import json as _json
        return _json.dumps({"intents": ["test intent one", "test intent two"]})

    def supports_thinking(self, model):
        return False

    def generate(self, prompt, model=None, system=None, stream=False, context=None):
        if stream:
            return iter(["<think>weighing stories</think>", "Recap ", "summary"])
        return "generated"

    def classify_query(self, query):
        return {"search_keywords": query, "time_cutoff_days": 0, "intent": "general", "entities": []}

    def get_embedding(self, text, model=None):
        return None


@pytest.fixture
def fake_ollama(monkeypatch):
    """Replace the OllamaClient used by the app with a deterministic fake."""
    from carrot import ollama_client
    monkeypatch.setattr(ollama_client, "OllamaClient", FakeOllamaClient)
    return FakeOllamaClient


@pytest.fixture
def no_background_workers(monkeypatch):
    """Keep app startup from running pollers against the test's database.

    Entering a `TestClient` runs the app's startup, which starts the proactive
    watcher and the research scheduler. The watcher's loop calls `run_checks()`
    *immediately*, before its first wait — so it raced every test that used a
    client, against the same database, on its own thread.

    It showed up as one flaky test. `test_notification_api_flow` creates an
    overdue reminder and asserts that an explicit check raises a notification
    for it; when the watcher got there first, the notification already existed,
    dedupe suppressed the second one, and the count came back 0. Whether it
    happened depended on thread scheduling, which is why it looked like noise.

    No test starts either worker or asserts on one, so nothing is lost by not
    running them here. A test that wants one can call it directly.

    Named daemons are no longer the mechanism, only the belt to the braces:
    each starter refuses under CARROT_NO_BACKGROUND, set below for the whole
    session. A fixture has to list every daemon, and the one it does not list
    is the one that breaks things — the scheduled-tasks thread was added,
    never listed here, and spent every full run writing to databases other
    tests had already torn down. It surfaced as a sqlite error in a different
    file each time, which reads as flakiness and was one unguarded thread.
    """
    import os

    from carrot import proactive, deep_research, scheduled

    monkeypatch.setenv("CARROT_NO_BACKGROUND", "1")
    monkeypatch.setattr(proactive, "start_watcher", lambda *a, **k: None)
    monkeypatch.setattr(deep_research, "start_scheduler", lambda *a, **k: None)
    monkeypatch.setattr(scheduled, "start_scheduler", lambda *a, **k: None)


@pytest.fixture
def client(isolated_db, fake_ollama, no_background_workers):
    """A TestClient wired to the isolated DB, mocked Ollama, and a session token."""
    from fastapi.testclient import TestClient
    from carrot import app as carrot_app, security

    with TestClient(
        carrot_app.app,
        headers={security.TOKEN_HEADER: security.session_token()},
    ) as c:
        yield c


@pytest.fixture
def unauthenticated_client(isolated_db, fake_ollama, no_background_workers):
    """A TestClient with no session token, for testing the auth gate itself."""
    from fastapi.testclient import TestClient
    from carrot import app as carrot_app

    with TestClient(carrot_app.app) as c:
        yield c
