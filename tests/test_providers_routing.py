"""Multi-provider BYOK, per-task assignments, and custom tasks."""
import json

import pytest

from carrot import config, providers, router
from carrot.openai_client import (
    _ToolCallAccumulator, to_openai_messages, to_openai_tools,
)


# ===== Provider registry =====

def test_builtin_providers_are_always_present(isolated_db):
    ids = {p["id"] for p in providers.list_providers()}
    assert {"ollama", "anthropic", "openai"} <= ids


def test_ollama_needs_no_key_and_is_usable_out_of_the_box(isolated_db):
    ollama = providers.get_provider("ollama")
    assert ollama["requires_key"] is False
    assert ollama["configured"] is True
    assert providers.usable("ollama") is True


def test_keyed_provider_is_not_usable_until_a_key_is_set(isolated_db, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert providers.usable("openai") is False
    providers.set_api_key("openai", "sk-test")
    assert providers.usable("openai") is True


def test_provider_key_falls_back_to_the_environment(isolated_db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert providers.api_key("openai") == "from-env"


def test_stored_key_beats_the_environment(isolated_db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    providers.set_api_key("openai", "from-config")
    assert providers.api_key("openai") == "from-config"


def test_legacy_anthropic_key_still_resolves(isolated_db, monkeypatch):
    """Installs configured before multi-provider support keep working."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.set_config("cloud_api_key", "legacy-key")
    assert providers.api_key("anthropic") == "legacy-key"
    assert router.cloud_api_key() == "legacy-key"


def test_custom_byok_provider_round_trips(isolated_db):
    created = providers.upsert_provider(
        "openrouter", label="OpenRouter", kind="openai",
        base_url="https://openrouter.ai/api/v1",
    )
    assert created["builtin"] is False
    assert created["base_url"] == "https://openrouter.ai/api/v1"
    assert created["kind"] == "openai"
    assert providers.get_provider("openrouter") is not None


def test_custom_provider_requires_a_base_url(isolated_db):
    with pytest.raises(providers.ProviderError):
        providers.upsert_provider("nobase", kind="openai")


def test_base_url_must_be_http(isolated_db):
    with pytest.raises(providers.ProviderError):
        providers.upsert_provider("bad", kind="openai", base_url="ftp://example.com")


def test_provider_id_is_validated(isolated_db):
    for bad in ("", "Has Caps", "with space", "x" * 41):
        with pytest.raises(providers.ProviderError):
            providers.upsert_provider(bad, kind="openai", base_url="https://e.com/v1")


def test_unknown_provider_kind_is_rejected(isolated_db):
    with pytest.raises(providers.ProviderError):
        providers.upsert_provider("weird", kind="telepathy", base_url="https://e.com/v1")


def test_builtins_cannot_be_deleted(isolated_db):
    with pytest.raises(providers.ProviderError):
        providers.delete_provider("ollama")


def test_deleting_a_custom_provider_forgets_its_key(isolated_db):
    providers.upsert_provider("groq", kind="openai", base_url="https://api.groq.com/openai/v1")
    providers.set_api_key("groq", "gsk-test")
    assert providers.delete_provider("groq") is True
    assert providers.get_provider("groq") is None
    assert providers.api_key("groq") == ""


def test_editing_a_builtin_records_an_override(isolated_db):
    providers.upsert_provider("openai", base_url="https://proxy.internal/v1")
    assert providers.get_provider("openai")["base_url"] == "https://proxy.internal/v1"
    # Stored as an override, not as a new custom provider.
    assert config.get_config()["custom_providers"] == []


def test_disabled_provider_is_not_usable(isolated_db):
    providers.set_api_key("openai", "sk-test")
    providers.set_enabled("openai", False)
    assert providers.usable("openai") is False


def test_registry_never_exposes_a_key(isolated_db):
    providers.set_api_key("openai", "sk-secret")
    blob = json.dumps(providers.list_providers())
    assert "sk-secret" not in blob
    assert providers.get_provider("openai")["key_set"] is True


def test_redaction_reduces_provider_keys_to_booleans(isolated_db):
    providers.set_api_key("openai", "sk-secret")
    redacted = config.redact(config.get_config())
    assert redacted["provider_keys"] == {"openai": True}
    assert "sk-secret" not in json.dumps(redacted)


# ===== Custom tasks =====

def test_builtin_tasks_are_listed(isolated_db):
    assert {t["id"] for t in router.tasks()} == set(router.TASKS)


def test_custom_task_becomes_routable(isolated_db):
    router.add_task("checking", label="Checking", description="Second-pass review")
    assert "checking" in router.task_ids()
    assert router.route("checking").task == "checking"


def test_custom_task_id_is_validated(isolated_db):
    for bad in ("", "two words", "x" * 41, "-lead"):
        with pytest.raises(ValueError):
            router.add_task(bad)


def test_task_and_provider_ids_are_normalized_not_rejected(isolated_db):
    """Typing a capital letter should not be an error — it should just work."""
    assert router.add_task("  Checking  ")["id"] == "checking"
    assert providers.upsert_provider(
        " MyHost ", kind="openai", base_url="https://e.com/v1"
    )["id"] == "myhost"


def test_builtin_task_cannot_be_shadowed_or_deleted(isolated_db):
    with pytest.raises(ValueError):
        router.add_task("chat")
    with pytest.raises(ValueError):
        router.delete_task("chat")


def test_deleting_a_custom_task_drops_its_assignment(isolated_db):
    router.add_task("checking")
    router.set_route("checking", "llama3.2:3b", provider="ollama")
    assert router.delete_task("checking") is True
    assert "checking" not in router.assignments()
    assert "checking" not in router.task_ids()


def test_custom_task_can_be_marked_local_only(isolated_db):
    router.add_task("triage", local_only=True)
    assert router.is_local_only("triage") is True
    assert router.route("triage", prefer_cloud=True).provider == router.PROVIDER_LOCAL


# ===== Assignments =====

def test_assignment_pins_a_task_to_a_provider(isolated_db):
    providers.set_api_key("openai", "sk-test")
    router.set_route(router.TASK_RECAP, "some-model", provider="openai")
    resolved = router.route(router.TASK_RECAP)
    assert resolved.provider == "openai"
    assert resolved.model == "some-model"
    assert resolved.kind == "openai"
    assert resolved.local is False


def test_different_tasks_can_use_different_providers(isolated_db):
    providers.set_api_key("openai", "sk-test")
    providers.upsert_provider("groq", kind="openai", base_url="https://api.groq.com/openai/v1")
    providers.set_api_key("groq", "gsk-test")
    router.add_task("checking")

    router.set_route(router.TASK_RECAP, "model-a", provider="openai")
    router.set_route("checking", "model-b", provider="groq")
    router.set_route(router.TASK_CHAT, "llama3.2:3b", provider="ollama")

    assert router.route(router.TASK_RECAP).provider == "openai"
    assert router.route("checking").provider == "groq"
    assert router.route(router.TASK_CHAT).provider == "ollama"


def test_assignment_beats_automatic_escalation(isolated_db):
    """A pin is explicit; escalation is a default. The pin wins."""
    config.set_config("cloud_enabled", True)
    config.set_config("cloud_api_key", "test-key")
    config.set_config("cloud_tasks", [router.TASK_REASONING])
    router.set_route(router.TASK_REASONING, "llama3.2:3b", provider="ollama")
    assert router.route(router.TASK_REASONING).provider == router.PROVIDER_LOCAL


def test_assignment_overrides_the_local_only_default(isolated_db):
    """LOCAL_ONLY_TASKS blocks auto-escalation, not a deliberate assignment."""
    providers.set_api_key("openai", "sk-test")
    router.set_route(router.TASK_CLASSIFY, "cheap-model", provider="openai")
    assert router.route(router.TASK_CLASSIFY).provider == "openai"


def test_assignment_to_an_unconfigured_provider_falls_back_to_local(isolated_db, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    router.set_route(router.TASK_RECAP, "some-model", provider="openai")
    resolved = router.route(router.TASK_RECAP)
    assert resolved.provider == router.PROVIDER_LOCAL
    assert "not configured" in resolved.reason


def test_assignment_to_an_unknown_provider_is_rejected(isolated_db):
    with pytest.raises(ValueError):
        router.set_route(router.TASK_CHAT, "some-model", provider="nope")


def test_set_route_requires_a_model(isolated_db):
    with pytest.raises(ValueError):
        router.set_route(router.TASK_CHAT, "")


def test_clear_route_restores_automatic_behavior(isolated_db):
    providers.set_api_key("openai", "sk-test")
    router.set_route(router.TASK_RECAP, "some-model", provider="openai")
    router.clear_route(router.TASK_RECAP)
    assert router.route(router.TASK_RECAP).provider == router.PROVIDER_LOCAL


def test_legacy_string_assignments_are_still_read(isolated_db):
    """Routes stored by an older build were bare model strings."""
    config.set_config("model_routes", {"chat": "llama3.2:3b", "code": "claude-opus-5"})
    assert router.route(router.TASK_CHAT).provider == router.PROVIDER_LOCAL
    assert router.route(router.TASK_CHAT).model == "llama3.2:3b"
    assert router.assignments()["code"]["provider"] == "anthropic"


def test_explicit_model_still_beats_an_assignment(isolated_db):
    providers.set_api_key("openai", "sk-test")
    router.set_route(router.TASK_CHAT, "some-model", provider="openai")
    resolved = router.route(router.TASK_CHAT, model="llama3.2:1b")
    assert resolved.model == "llama3.2:1b"
    assert resolved.reason == "explicitly selected"


def test_explicit_provider_is_honored_without_name_sniffing(isolated_db):
    providers.upsert_provider("groq", kind="openai", base_url="https://api.groq.com/openai/v1")
    resolved = router.route(router.TASK_CHAT, model="claude-shaped-name", provider="groq")
    assert resolved.provider == "groq"
    assert resolved.kind == "openai"


# ===== OpenAI wire format =====

def test_tool_call_fragments_are_reassembled():
    accumulator = _ToolCallAccumulator()
    accumulator.feed([{"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": '{"pa'}}])
    accumulator.feed([{"index": 0, "function": {"arguments": 'th": "a.txt"}'}}])
    calls = accumulator.result()
    assert calls == [{
        "id": "call_1", "type": "function",
        "function": {"name": "read_file", "arguments": {"path": "a.txt"}},
    }]


def test_malformed_tool_arguments_degrade_to_empty():
    accumulator = _ToolCallAccumulator()
    accumulator.feed([{"index": 0, "id": "c", "function": {"name": "f", "arguments": "{not json"}}])
    assert accumulator.result()[0]["function"]["arguments"] == {}


def test_tool_results_convert_to_the_tool_role():
    converted = to_openai_messages([
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": {"path": "a"}}},
        ]},
        {"role": "tool", "content": "file body", "tool_call_id": "c1"},
    ])
    assert converted[0]["role"] == "system"
    assert converted[2]["tool_calls"][0]["function"]["arguments"] == '{"path": "a"}'
    assert converted[3] == {"role": "tool", "tool_call_id": "c1", "content": "file body"}


class FakeStreamResponse:
    """A stand-in for a streamed requests response."""

    def __init__(self, lines, status_code=200, payload=None):
        self.lines = lines
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def iter_lines(self, decode_unicode=False):
        return iter(self.lines)

    def json(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _sse(chunks):
    return [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]


def test_openai_stream_splits_reasoning_content_and_tool_calls(monkeypatch):
    from carrot import openai_client

    lines = _sse([
        {"choices": [{"delta": {"reasoning_content": "thinking hard"}}]},
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [{"delta": {"content": " there"}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a"}'}},
        ]}}]},
    ])
    monkeypatch.setattr(openai_client.requests, "post", lambda *a, **k: FakeStreamResponse(lines))

    client = openai_client.OpenAICompatibleClient("https://api.example.com/v1", "sk-test")
    events = list(client.chat_stream_events([{"role": "user", "content": "hi"}], model="m"))

    assert {"type": "thinking", "text": "thinking hard"} in events
    assert "".join(e["text"] for e in events if e["type"] == "content") == "Hello there"
    calls = [e for e in events if e["type"] == "tool_calls"]
    assert calls[0]["calls"][0]["function"] == {"name": "read_file", "arguments": {"path": "a"}}


def test_openai_stream_routes_think_tags_to_the_thinking_channel(monkeypatch):
    """Models that inline reasoning in <think> tags get split like Ollama's do."""
    from carrot import openai_client

    lines = _sse([
        {"choices": [{"delta": {"content": "<think>weighing"}}]},
        {"choices": [{"delta": {"content": " options</think>Answer"}}]},
    ])
    monkeypatch.setattr(openai_client.requests, "post", lambda *a, **k: FakeStreamResponse(lines))

    client = openai_client.OpenAICompatibleClient("https://api.example.com/v1", "sk-test")
    events = list(client.chat_stream_events([{"role": "user", "content": "hi"}], model="m"))

    assert "weighing options" in "".join(e["text"] for e in events if e["type"] == "thinking")
    assert "".join(e["text"] for e in events if e["type"] == "content") == "Answer"


def test_openai_error_surfaces_the_provider_message(monkeypatch):
    from carrot import openai_client

    response = FakeStreamResponse([], status_code=401,
                                  payload={"error": {"message": "Incorrect API key"}})
    monkeypatch.setattr(openai_client.requests, "post", lambda *a, **k: response)

    client = openai_client.OpenAICompatibleClient("https://api.example.com/v1", "bad")
    with pytest.raises(RuntimeError, match="Incorrect API key"):
        list(client.chat_stream_events([{"role": "user", "content": "hi"}], model="m"))


def test_router_streams_an_assigned_openai_provider(isolated_db, monkeypatch):
    """End to end: an assignment sends the turn out over the OpenAI wire."""
    from carrot import openai_client

    providers.upsert_provider("groq", kind="openai", base_url="https://api.groq.com/openai/v1")
    providers.set_api_key("groq", "gsk-test")
    router.set_route(router.TASK_RECAP, "some-model", provider="groq")

    seen = {}

    def fake_post(url, headers=None, json=None, **kwargs):
        seen["url"] = url
        seen["headers"] = headers
        seen["model"] = json["model"]
        return FakeStreamResponse(_sse([{"choices": [{"delta": {"content": "ok"}}]}]))

    monkeypatch.setattr(openai_client.requests, "post", fake_post)

    resolved = router.route(router.TASK_RECAP)
    events = list(router.stream_events(resolved, [{"role": "user", "content": "hi"}]))

    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer gsk-test"
    assert seen["model"] == "some-model"
    assert events == [{"type": "content", "text": "ok"}]


def test_tools_convert_to_the_function_envelope():
    converted = to_openai_tools([
        {"type": "function", "function": {"name": "f", "description": "d",
                                          "parameters": {"type": "object", "properties": {}}}},
    ])
    assert converted[0]["type"] == "function"
    assert converted[0]["function"]["name"] == "f"


# ===== API =====

def test_providers_endpoint_lists_builtins(client):
    body = client.get("/api/router/providers").json()
    assert {p["id"] for p in body["providers"]} >= {"ollama", "anthropic", "openai"}
    assert body["presets"]


def test_provider_can_be_added_and_removed_over_the_api(client):
    created = client.post("/api/router/providers", json={
        "id": "myhost", "label": "My Host", "kind": "openai",
        "base_url": "http://localhost:9000/v1", "api_key": "secret-key",
    })
    assert created.status_code == 200
    assert created.json()["key_set"] is True
    assert "secret-key" not in created.text

    assert client.delete("/api/router/providers/myhost").status_code == 200
    assert client.delete("/api/router/providers/myhost").status_code == 404


def test_builtin_provider_delete_is_refused(client):
    assert client.delete("/api/router/providers/ollama").status_code == 400


def test_provider_key_endpoint_never_echoes_the_key(client):
    response = client.put("/api/router/providers/openai/key", json={"api_key": "sk-secret"})
    assert response.status_code == 200
    assert "sk-secret" not in response.text
    assert response.json()["key_set"] is True


def test_config_endpoint_redacts_provider_keys(client):
    client.put("/api/router/providers/openai/key", json={"api_key": "sk-secret"})
    body = client.get("/api/config").json()
    assert body["provider_keys"] == {"openai": True}
    assert "sk-secret" not in json.dumps(body)


def test_route_assignment_over_the_api(client):
    client.put("/api/router/providers/openai/key", json={"api_key": "sk-test"})
    response = client.put("/api/router/route", json={
        "task": "recap", "provider": "openai", "model": "some-model",
    })
    assert response.status_code == 200
    assert response.json()["route"]["provider"] == "openai"

    assert client.delete("/api/router/route/recap").json()["route"]["local"] is True


def test_unknown_task_assignment_is_a_400(client):
    response = client.put("/api/router/route", json={"task": "nope", "model": "m"})
    assert response.status_code == 400


def test_custom_task_lifecycle_over_the_api(client):
    created = client.post("/api/router/tasks", json={"id": "checking", "label": "Checking"})
    assert created.status_code == 200
    assert created.json()["builtin"] is False

    tasks = client.get("/api/router/tasks").json()["tasks"]
    assert "checking" in {t["id"] for t in tasks}
    assert "checking" in client.get("/api/router/status").json()["routes"]

    assert client.delete("/api/router/tasks/checking").status_code == 200
    assert client.delete("/api/router/tasks/checking").status_code == 404


def test_builtin_task_delete_is_refused(client):
    assert client.delete("/api/router/tasks/chat").status_code == 400


def test_invalid_custom_task_id_is_a_400(client):
    assert client.post("/api/router/tasks", json={"id": "Two Words"}).status_code == 400


def test_router_status_reports_providers_and_tasks(client):
    body = client.get("/api/router/status").json()
    assert body["providers"] and body["tasks"]
    assert set(body["routes"]) == set(router.TASKS)
    assert body["escalation_provider"] == "anthropic"


def test_provider_test_endpoint_reports_a_missing_key(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = client.post("/api/router/providers/openai/test").json()
    assert body["ok"] is False
    assert "no API key" in body["error"]


# ===== Cloud models must reach the chat model picker =====

def test_models_endpoint_includes_configured_provider_models(client, monkeypatch):
    """A key you already pasted is useless if the picker only offers Ollama."""
    from carrot import app as app_mod

    monkeypatch.setattr(app_mod.router_mod, "status", lambda: {"providers": [
        {"id": "ollama", "label": "Ollama", "enabled": True, "configured": True},
        {"id": "mistral", "label": "Mistral", "enabled": True, "configured": True},
        {"id": "openai", "label": "OpenAI", "enabled": True, "configured": False},
        {"id": "groq", "label": "Groq", "enabled": False, "configured": True},
    ]})
    monkeypatch.setattr(app_mod.providers_mod, "list_models",
                        lambda pid: {"models": [f"{pid}-large", f"{pid}-small"]})

    data = client.get("/api/models").json()
    groups = {g["provider"]: g for g in data["remote"]}
    # Configured and enabled -> listed. Ollama is the local section already.
    assert "mistral" in groups
    assert groups["mistral"]["models"] == ["mistral-large", "mistral-small"]
    assert "ollama" not in groups
    # Unconfigured or disabled providers are not offered.
    assert "openai" not in groups and "groq" not in groups


def test_models_endpoint_reports_which_route_serves_chat(client, monkeypatch):
    from carrot import app as app_mod

    from carrot import config as config_mod
    monkeypatch.setattr(app_mod.router_mod, "status", lambda: {"providers": []})
    # A pin only takes effect for a provider that actually has a key.
    config_mod.set_config("provider_keys", {"anthropic": "sk-test"})
    app_mod.router_mod.set_route("chat", provider="anthropic", model="claude-opus-5")
    data = client.get("/api/models").json()
    assert data["chat_provider"] == "anthropic"
    assert data["chat_model"] == "claude-opus-5"
    assert data["chat_local"] is False

    app_mod.router_mod.clear_route("chat")
    data = client.get("/api/models").json()
    assert data["chat_local"] is True


def test_models_endpoint_survives_a_broken_provider(client, monkeypatch):
    """One provider failing to list must not empty the whole picker."""
    from carrot import app as app_mod

    monkeypatch.setattr(app_mod.router_mod, "status", lambda: {"providers": [
        {"id": "good", "label": "Good", "enabled": True, "configured": True},
        {"id": "bad", "label": "Bad", "enabled": True, "configured": True},
    ]})

    def flaky(pid):
        if pid == "bad":
            raise RuntimeError("network down")
        return {"models": ["good-1"]}
    monkeypatch.setattr(app_mod.providers_mod, "list_models", flaky)

    data = client.get("/api/models").json()
    assert [g["provider"] for g in data["remote"]] == ["good"]
    assert data["installed"] is not None
