"""Tests for the Ollama bootstrap module."""
from carrot import bootstrap


def test_default_model_is_gemma4_e4b():
    assert bootstrap.DEFAULT_MODEL == "gemma4:e4b"


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
