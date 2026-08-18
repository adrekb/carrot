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


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path_factory, monkeypatch):
    """Every test gets its own data directory, whether it asks for one or not.

    Autouse because opting in was the wrong default. A test that never
    requested `isolated_db` did not run without a database — it ran against
    the developer's real one, at `carrot/data/carrot.db`, and so did every
    other test that had not opted in. That is one mutable file shared by a few
    hundred tests: rows from one land in another's queries, and whether that
    matters depends on what ran first and on what the previous run left
    behind. It is the shape of a suite that passes four times and then fails
    on a different unrelated file, each of those files passing alone.

    Its own directory from `tmp_path_factory`, deliberately *not* under the
    test's `tmp_path`. Several tests — the git and checkpoint ones — run
    `git init` in `tmp_path` and then `git add -A`. Anything the harness leaves
    in there becomes a tracked file of the repository under test: the database
    got committed into checkpoints, and restoring one ran `checkout-index -f`
    over a SQLite file the suite still had open, which on Windows cannot be
    unlinked. It failed only when a connection happened to be open at that
    moment, which is what made it look like flakiness rather than like two
    things sharing a directory. A sibling directory cannot be reached by
    `git add -A` at all, which is the version of this fix that does not depend
    on remembering the hazard.

    The schema is not created here. `get_db()` builds it on first connect, so
    a test that never touches the database pays nothing for this.
    """
    from carrot import security

    data = tmp_path_factory.mktemp("carrot-data")
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
    # Notes are files, and they bind their directory at import time — so
    # patching `config.CARROT_DIR` does not move them. Without this line the
    # suite reads and writes the developer's real notes, which is the same
    # failure the database had and just as invisible: a listing test passes
    # either way, it just happens to be listing somebody's actual writing.
    from carrot import notes

    monkeypatch.setattr(notes, "NOTES_DIR", str(data / "notes"))
    return db_path


# The machine the suite pretends to be.
#
# The default model is chosen from the hardware now, which means that without
# this the suite's answers depend on the laptop it runs on: the same assertion
# passes on a developer with 8 GB and fails on one with a 24 GB card, and CI
# gets a third answer. A pinned profile makes "the default is gemma4:e4b" a
# statement about a stated machine rather than about whoever ran the tests.
#
# 6 GB is chosen so that it is: the catalog's best all-rounder inside that
# budget is gemma4:e4b, which is what the app shipped for everyone before it
# learned to ask. Tests that care about other machines patch `detect_specs`
# themselves — see test_default_model.py.
TEST_MACHINE = {
    "os": "Linux", "cpu": "test cpu", "cpu_cores": 8, "ram_gb": 12.0,
    "gpu": None, "vram_gb": 0.0, "backend": "cpu", "model_budget_gb": 6.0,
}


@pytest.fixture(autouse=True)
def pinned_hardware(monkeypatch):
    """Fixed specs, and an empty memo, for every test.

    The memo is the reason this is autouse rather than opt-in. It is a module
    global with a five-minute life, so one test that patches the detector
    leaves its answer sitting there for whatever runs next — the same
    shared-mutable-state failure as a shared database, in miniature."""
    from carrot import hub

    hub._specs_memo.clear()
    monkeypatch.setattr(hub, "detect_specs", lambda refresh=False: dict(TEST_MACHINE))


@pytest.fixture
def isolated_db(temp_data_dir):
    """The temporary database, with the schema already created.

    The redirection itself is now autouse; this is the opt-in for tests that
    want the tables to exist before they run their first query.
    """
    database.init_db()
    return temp_data_dir


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


@pytest.fixture(autouse=True)
def no_live_search(monkeypatch):
    """No test may reach a real search provider.

    Exa's free endpoint is metered per IP per day. A suite that calls it for
    real spends the developer's own allowance on every run, goes red on a
    train, and — worst — passes for reasons that have nothing to do with the
    code. One test was already doing it: `_raw_search` became the provider
    chain, so a case written to check which DuckDuckGo client is preferred
    quietly started making live calls to Exa instead.

    Raising rather than returning [] on purpose: an empty result is a state
    the callers handle, so a test that hit the network would still pass and
    the leak would stay hidden. Tests that want a provider patch it.

    Only Exa is blocked. `_ddg_search` goes through a client library the tests
    already replace in `sys.modules`, so it does not reach the network on its
    own — and blocking it here broke the one case that legitimately drives it
    with a fake client.
    """
    from carrot import websearch

    def refuse(*a, **k):
        raise AssertionError(
            "a test tried to reach Exa — patch websearch._exa_search, or "
            "websearch.search, instead")

    monkeypatch.setattr(websearch, "_exa_search", refuse)
