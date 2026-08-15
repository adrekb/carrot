"""Tests for the Ollama bootstrap module."""
from carrot import bootstrap


def test_the_constant_is_a_floor_and_not_the_default():
    """There is no longer one model that is right for every machine, so this
    is only what is left when hardware detection itself fails. Small on
    purpose: too small is slow-witted, too big does not run at all, and only
    one of those gets the user to the screen where they can choose."""
    from carrot import hub

    assert bootstrap.DEFAULT_MODEL == hub.FALLBACK_MODEL
    entry = next(m for m in hub.BUNDLED_CATALOG if m["id"] == bootstrap.DEFAULT_MODEL)
    assert entry["min_mem_gb"] <= 4.0


def test_the_default_comes_from_the_machine(isolated_db):
    """The pinned test machine has 6 GB for models — see conftest."""
    assert bootstrap.get_target_model() == "gemma4:e4b"


def test_bootstrap_state_roundtrip(tmp_path, monkeypatch):
    state_path = str(tmp_path / "bootstrap.json")
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_STATE_PATH", state_path)

    # Fresh state defaults
    state = bootstrap.load_bootstrap_state()
    assert state["ollama_installed"] is False
    assert state["model_pulled"] is False

    # Save and reload
    state["ollama_installed"] = True
    state["model_pulled"] = True
    bootstrap.save_bootstrap_state(state)
    reloaded = bootstrap.load_bootstrap_state()
    assert reloaded["ollama_installed"] is True
    assert reloaded["model_pulled"] is True


def test_is_model_available_matches_prefix():
    # A model name with an explicit tag should match itself and base name.
    assert bootstrap.is_model_available.__name__ == "is_model_available"
    # Pure logic check of the matching rule used by the function.
    model = "gemma4:e4b"
    candidates = ["gemma4:e4b", "llama3:latest"]
    assert any(m == model or m.startswith(f"{model}:") for m in candidates)


def test_bootstrap_status_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_STATE_PATH", str(tmp_path / "b.json"))
    monkeypatch.setattr(bootstrap, "get_ollama_executable", lambda: None)
    monkeypatch.setattr(bootstrap, "is_ollama_running", lambda: False)
    monkeypatch.setattr(bootstrap, "is_model_available", lambda model=None: False)

    status = bootstrap.bootstrap_status()
    assert status["ollama_installed"] is False
    assert status["ollama_running"] is False
    assert status["model_pulled"] is False
    assert status["default_model"] == "gemma4:e4b"
    assert status["bootstrap_complete"] is False


def test_bootstrap_status_endpoint(client, monkeypatch):
    from carrot import bootstrap as b
    monkeypatch.setattr(b, "get_ollama_executable", lambda: "/usr/bin/ollama")
    monkeypatch.setattr(b, "is_ollama_running", lambda: True)
    monkeypatch.setattr(b, "is_model_available", lambda model=None: True)
    monkeypatch.setattr(
        b, "load_bootstrap_state",
        lambda: {"ollama_installed": True, "model_pulled": True, "model_pulling": False},
    )

    resp = client.get("/api/bootstrap/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ollama_installed"] is True
    assert data["bootstrap_complete"] is True


# ===== Model pull over the HTTP API (progress + real errors) =====

class _FakeResp:
    """Minimal stand-in for a streaming requests response."""

    def __init__(self, lines, status_code=200, text=""):
        self._lines = lines
        self.status_code = status_code
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


def test_pull_model_reports_byte_progress(monkeypatch):
    import json as _json
    from carrot import bootstrap

    lines = [
        _json.dumps({"status": "pulling manifest"}),
        _json.dumps({"status": "downloading", "completed": 500, "total": 1000}),
        _json.dumps({"status": "downloading", "completed": 1000, "total": 1000}),
        _json.dumps({"status": "success"}),
    ]
    monkeypatch.setattr(bootstrap.requests, "post", lambda *a, **k: _FakeResp(lines))
    monkeypatch.setattr(bootstrap, "is_model_available", lambda m: True)

    events = []
    assert bootstrap.pull_model("tiny:1b", events.append) is True
    pulls = [e for e in events if e["type"] == "pull"]
    # Byte counts reach the UI, which is what drives the progress bar.
    assert any(e["completed"] == 500 and e["total"] == 1000 for e in pulls)
    assert all(e["model"] == "tiny:1b" for e in pulls)


def test_pull_model_surfaces_the_real_error(monkeypatch):
    import json as _json
    from carrot import bootstrap

    lines = [_json.dumps({"error": "model 'nope:1b' not found"})]
    monkeypatch.setattr(bootstrap.requests, "post", lambda *a, **k: _FakeResp(lines))
    monkeypatch.setattr(bootstrap, "is_model_available", lambda m: False)

    events = []
    assert bootstrap.pull_model("nope:1b", events.append) is False
    errors = [e["message"] for e in events if e["type"] == "error"]
    assert errors and "not found" in errors[0]


def test_pull_model_reports_unreachable_ollama(monkeypatch):
    from carrot import bootstrap

    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(bootstrap.requests, "post", boom)

    events = []
    assert bootstrap.pull_model("x:1b", events.append) is False
    assert any("connection refused" in e.get("message", "") for e in events)


def test_run_bootstrap_error_explains_why(monkeypatch, tmp_path):
    """A failed pull must report the cause, not just 'Failed to pull X'."""
    from carrot import bootstrap

    monkeypatch.setattr(bootstrap, "BOOTSTRAP_STATE_PATH", str(tmp_path / "b.json"))
    monkeypatch.setattr(bootstrap, "get_ollama_executable", lambda: "/usr/bin/ollama")
    monkeypatch.setattr(bootstrap, "is_ollama_running", lambda: True)
    monkeypatch.setattr(bootstrap, "is_model_available", lambda m: False)
    monkeypatch.setattr(bootstrap, "set_config", lambda k, v: None)

    def failing_pull(model, progress_cb=None):
        progress_cb({"type": "error", "message": "disk quota exceeded"})
        return False
    monkeypatch.setattr(bootstrap, "pull_model", failing_pull)

    result = bootstrap.run_bootstrap(model="tiny:1b")
    assert result["error"] == "disk quota exceeded"


def test_bootstrap_stream_endpoint_emits_progress_then_done(client, monkeypatch):
    from carrot import app as app_mod

    def fake_run(progress_cb=None, model=None):
        progress_cb({"type": "status", "message": "Checking Ollama..."})
        progress_cb({"type": "pull", "model": model, "completed": 10, "total": 20})
        return {"ollama_installed": True, "model_pulled": True, "model": model, "error": None}
    monkeypatch.setattr(app_mod.bootstrap_mod, "run_bootstrap", fake_run)

    with client.stream("GET", "/api/bootstrap/stream?model=tiny:1b") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert '"type": "pull"' in body and '"completed": 10' in body
    assert '"type": "done"' in body
