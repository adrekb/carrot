"""Shared pytest fixtures for the Carrot test suite.

Provides an isolated SQLite database (via monkeypatched module paths) and a
FastAPI ``TestClient`` with a mocked Ollama client so tests never require a
running Ollama server.
"""
import os
import pytest

from carrot import database, config


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point the database and config modules at a temporary SQLite file."""
    from carrot import security

    db_path = str(tmp_path / "carrot.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DBCORE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CARROT_DIR", str(tmp_path))
    # Keep the session token out of the real data directory too.
    monkeypatch.setattr(security, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(security, "TOKEN_PATH", str(tmp_path / "config" / "session.json"))
    monkeypatch.setattr(security, "_token", None)
    database.init_db()
    return db_path


class FakeOllamaClient:
    """A stand-in for OllamaClient that needs no running server."""

    def __init__(self, *args, **kwargs):
        self.default_model = "gemma4:e4b"

    def is_available(self):
        return True

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
def client(isolated_db, fake_ollama):
    """A TestClient wired to the isolated DB, mocked Ollama, and a session token."""
    from fastapi.testclient import TestClient
    from carrot import app as carrot_app, security

    with TestClient(
        carrot_app.app,
        headers={security.TOKEN_HEADER: security.session_token()},
    ) as c:
        yield c


@pytest.fixture
def unauthenticated_client(isolated_db, fake_ollama):
    """A TestClient with no session token, for testing the auth gate itself."""
    from fastapi.testclient import TestClient
    from carrot import app as carrot_app

    with TestClient(carrot_app.app) as c:
        yield c
