"""Model providers — bring your own key, for whichever service you use.

Carrot routes *tasks* to models, and a model lives behind a provider. A
provider is a wire protocol plus an endpoint plus (usually) a key:

* ``ollama``    — the local daemon; no key, never leaves the machine
* ``anthropic`` — the Anthropic Messages API, via the official SDK
* ``openai``    — the OpenAI chat-completions wire format

That last one is the important one. Dozens of services speak it — OpenAI
itself, OpenRouter, Groq, Together, DeepSeek, Mistral, vLLM, LM Studio,
llama.cpp's server — so "add a provider" is just "name it, give it a base URL
and a key". Nothing about a custom provider is special-cased anywhere else in
Carrot; it routes and streams exactly like the built-ins.

Keys are stored in the local config under ``provider_keys`` and redacted by the
read-only config endpoint. They can also come from the environment, so a user
who already exports ``OPENAI_API_KEY`` does not have to paste it again.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import requests

from .config import get_config, set_config

# Wire protocols a provider can speak. The router dispatches on these.
KIND_OLLAMA = "ollama"
KIND_ANTHROPIC = "anthropic"
KIND_OPENAI = "openai"
KINDS = (KIND_OLLAMA, KIND_ANTHROPIC, KIND_OPENAI)

# Providers that ship with Carrot. Users can override the endpoint or disable
# them, but they cannot be deleted — the router falls back to Ollama and needs
# it to exist.
BUILTIN_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "ollama": {
        "id": "ollama",
        "label": "Ollama (on-device)",
        "kind": KIND_OLLAMA,
        "base_url": "",  # read from ollama_host so the existing setting stays authoritative
        "requires_key": False,
        "env_var": "",
        "docs": "https://ollama.com",
    },
    "anthropic": {
        "id": "anthropic",
        "label": "Anthropic",
        "kind": KIND_ANTHROPIC,
        "base_url": "https://api.anthropic.com",
        "requires_key": True,
        "env_var": "ANTHROPIC_API_KEY",
        "docs": "https://console.anthropic.com",
    },
    "openai": {
        "id": "openai",
        "label": "OpenAI",
        "kind": KIND_OPENAI,
        "base_url": "https://api.openai.com/v1",
        "requires_key": True,
        "env_var": "OPENAI_API_KEY",
        "docs": "https://platform.openai.com",
    },
    # Gemini through its OpenAI-compatibility layer, so it needs no separate
    # protocol implementation.
    "google": {
        "id": "google",
        "label": "Google (Gemini)",
        "kind": KIND_OPENAI,
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "requires_key": True,
        "env_var": "GEMINI_API_KEY",
        "docs": "https://aistudio.google.com/apikey",
    },
}

LOCAL_PROVIDER = "ollama"

# Presets offered in the "add a provider" form. Each is just an OpenAI-wire
# provider with a base URL filled in — there is no code behind them.
PRESETS: List[Dict[str, str]] = [
    {"id": "openrouter", "label": "OpenRouter", "kind": KIND_OPENAI,
     "base_url": "https://openrouter.ai/api/v1", "env_var": "OPENROUTER_API_KEY"},
    {"id": "groq", "label": "Groq", "kind": KIND_OPENAI,
     "base_url": "https://api.groq.com/openai/v1", "env_var": "GROQ_API_KEY"},
    {"id": "together", "label": "Together AI", "kind": KIND_OPENAI,
     "base_url": "https://api.together.xyz/v1", "env_var": "TOGETHER_API_KEY"},
    {"id": "deepseek", "label": "DeepSeek", "kind": KIND_OPENAI,
     "base_url": "https://api.deepseek.com/v1", "env_var": "DEEPSEEK_API_KEY"},
    {"id": "mistral", "label": "Mistral", "kind": KIND_OPENAI,
     "base_url": "https://api.mistral.ai/v1", "env_var": "MISTRAL_API_KEY"},
    {"id": "lmstudio", "label": "LM Studio (local)", "kind": KIND_OPENAI,
     "base_url": "http://localhost:1234/v1", "env_var": ""},
    {"id": "vllm", "label": "vLLM (local)", "kind": KIND_OPENAI,
     "base_url": "http://localhost:8000/v1", "env_var": ""},
    {"id": "custom", "label": "Custom (OpenAI-compatible)", "kind": KIND_OPENAI,
     "base_url": "", "env_var": ""},
]

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# Bumped on every change to the registry. Callers that cache anything derived
# from a provider — model lists, mostly — mix this into their cache key, so
# pasting a key shows that provider's models immediately instead of after a TTL.
_registry_version = 0


def registry_version() -> int:
    return _registry_version


def _store(key, value):
    """Persist a registry change and invalidate anything cached from it."""
    global _registry_version
    set_config(key, value)
    _registry_version += 1


class ProviderError(ValueError):
    """A provider could not be created, found, or used. Message is UI-safe."""


def validate_id(provider_id: str) -> str:
    provider_id = (provider_id or "").strip().lower()
    if not ID_PATTERN.match(provider_id):
        raise ProviderError(
            "provider id must be 1-40 characters of lowercase letters, digits, '-' or '_'"
        )
    return provider_id


# ===== Registry =====

def _custom() -> List[Dict[str, Any]]:
    raw = get_config().get("custom_providers", [])
    return [p for p in raw if isinstance(p, dict) and p.get("id")]


def _overrides() -> Dict[str, Dict[str, Any]]:
    raw = get_config().get("provider_settings", {})
    return raw if isinstance(raw, dict) else {}


def _keys() -> Dict[str, str]:
    raw = get_config().get("provider_keys", {})
    return raw if isinstance(raw, dict) else {}


def api_key(provider_id: str) -> str:
    """The key for a provider: stored value, then legacy value, then env.

    The legacy step matters — installs that configured Anthropic before
    multi-provider support keep working without the user touching anything.
    """
    stored = _keys().get(provider_id)
    if stored:
        return stored
    if provider_id == "anthropic":
        legacy = get_config().get("cloud_api_key", "")
        if legacy:
            return legacy
    spec = BUILTIN_PROVIDERS.get(provider_id) or _find_raw(provider_id) or {}
    env_var = spec.get("env_var") or f"CARROT_{provider_id.upper().replace('-', '_')}_API_KEY"
    return os.environ.get(env_var, "")


def _find_raw(provider_id: str) -> Optional[Dict[str, Any]]:
    for provider in _custom():
        if provider.get("id") == provider_id:
            return provider
    return None


def _hydrate(spec: Dict[str, Any], builtin: bool) -> Dict[str, Any]:
    """Apply user overrides and derived state to one provider record."""
    provider = dict(spec)
    provider.update(_overrides().get(provider["id"], {}))
    provider["builtin"] = builtin
    provider.setdefault("requires_key", provider.get("kind") != KIND_OLLAMA)
    provider.setdefault("models", [])
    provider.setdefault("env_var", "")
    provider.setdefault("docs", "")

    if provider["id"] == LOCAL_PROVIDER:
        # The local endpoint has always lived in ollama_host; keep one source.
        provider["base_url"] = get_config().get("ollama_host", "http://localhost:11434")

    key = api_key(provider["id"])
    provider["configured"] = bool(key) or not provider["requires_key"]
    provider["key_set"] = bool(key)
    provider["enabled"] = bool(provider.get("enabled", True))
    provider.pop("api_key", None)  # never surface a secret through the registry
    return provider


def list_providers(include_disabled: bool = True) -> List[Dict[str, Any]]:
    """Every provider, built-ins first, with keys resolved to booleans."""
    providers = [_hydrate(spec, builtin=True) for spec in BUILTIN_PROVIDERS.values()]
    known = {p["id"] for p in providers}
    for spec in _custom():
        if spec["id"] in known:
            continue
        providers.append(_hydrate(spec, builtin=False))
        known.add(spec["id"])
    if not include_disabled:
        providers = [p for p in providers if p["enabled"]]
    return providers


def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    if provider_id in BUILTIN_PROVIDERS:
        return _hydrate(BUILTIN_PROVIDERS[provider_id], builtin=True)
    spec = _find_raw(provider_id)
    return _hydrate(spec, builtin=False) if spec else None


def require_provider(provider_id: str) -> Dict[str, Any]:
    provider = get_provider(provider_id)
    if provider is None:
        raise ProviderError(f"unknown provider: {provider_id}")
    return provider


def usable(provider_id: str) -> bool:
    """Whether a route may resolve to this provider right now."""
    provider = get_provider(provider_id)
    return bool(provider and provider["enabled"] and provider["configured"])


# ===== Mutation =====

def upsert_provider(
    provider_id: str,
    label: str = "",
    kind: str = KIND_OPENAI,
    base_url: str = "",
    models: Optional[List[str]] = None,
    env_var: str = "",
) -> Dict[str, Any]:
    """Create or update a BYOK provider.

    Built-ins are not replaced wholesale — editing one records an override so a
    future Carrot release can still change its defaults.
    """
    provider_id = validate_id(provider_id)
    if kind not in KINDS:
        raise ProviderError(f"unknown provider kind: {kind}")
    if kind != KIND_OLLAMA and not base_url and provider_id not in BUILTIN_PROVIDERS:
        raise ProviderError("a base URL is required")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ProviderError("base URL must start with http:// or https://")

    patch: Dict[str, Any] = {}
    if label:
        patch["label"] = label
    if base_url:
        patch["base_url"] = base_url.rstrip("/")
    if models is not None:
        patch["models"] = [m for m in models if m]
    if env_var:
        patch["env_var"] = env_var

    if provider_id in BUILTIN_PROVIDERS:
        overrides = dict(_overrides())
        overrides[provider_id] = {**overrides.get(provider_id, {}), **patch}
        _store("provider_settings", overrides)
        return require_provider(provider_id)

    providers = _custom()
    for existing in providers:
        if existing["id"] == provider_id:
            existing.update(patch)
            break
    else:
        providers.append({
            "id": provider_id,
            "label": label or provider_id,
            "kind": kind,
            "base_url": (base_url or "").rstrip("/"),
            "requires_key": kind != KIND_OLLAMA,
            "env_var": env_var,
            "models": models or [],
            "enabled": True,
        })
    _store("custom_providers", providers)
    return require_provider(provider_id)


def delete_provider(provider_id: str) -> bool:
    """Remove a custom provider and forget its key. Built-ins cannot be deleted."""
    if provider_id in BUILTIN_PROVIDERS:
        raise ProviderError(f"{provider_id} is built in and cannot be deleted")
    providers = [p for p in _custom() if p["id"] != provider_id]
    if len(providers) == len(_custom()):
        return False
    _store("custom_providers", providers)
    set_api_key(provider_id, "")
    return True


def set_api_key(provider_id: str, key: str) -> bool:
    """Store (or clear, with an empty string) a provider's key."""
    keys = dict(_keys())
    if key:
        keys[provider_id] = key
    else:
        keys.pop(provider_id, None)
    _store("provider_keys", keys)
    # Keep the legacy field in step so an older build reading it still works.
    if provider_id == "anthropic":
        set_config("cloud_api_key", key)
    return bool(key)


def set_enabled(provider_id: str, enabled: bool) -> Dict[str, Any]:
    require_provider(provider_id)
    if provider_id in BUILTIN_PROVIDERS:
        overrides = dict(_overrides())
        overrides[provider_id] = {**overrides.get(provider_id, {}), "enabled": bool(enabled)}
        _store("provider_settings", overrides)
    else:
        providers = _custom()
        for existing in providers:
            if existing["id"] == provider_id:
                existing["enabled"] = bool(enabled)
        _store("custom_providers", providers)
    return require_provider(provider_id)


# ===== Discovery =====

def list_models(provider_id: str) -> Dict[str, Any]:
    """Ask a provider what it can serve.

    Model names go stale fast, so Carrot never ships a hardcoded list — the
    picker asks the endpoint. Failures are returned, not raised, because a
    settings page should still render when one provider is unreachable.
    """
    provider = require_provider(provider_id)
    try:
        if provider["kind"] == KIND_OLLAMA:
            from .ollama_client import OllamaClient

            models = [m.get("name") for m in OllamaClient().list_models() if m.get("name")]
        elif provider["kind"] == KIND_ANTHROPIC:
            models = _list_anthropic_models(provider)
        else:
            models = _list_openai_models(provider)
    except Exception as exc:
        return {"provider": provider_id, "models": provider.get("models", []), "error": str(exc)}
    if not models:
        models = provider.get("models", [])
    return {"provider": provider_id, "models": sorted(set(models))}


def _list_anthropic_models(provider: Dict[str, Any]) -> List[str]:
    key = api_key(provider["id"])
    if not key:
        raise ProviderError("no API key configured")
    response = requests.get(
        f"{provider['base_url'].rstrip('/')}/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        timeout=15,
    )
    response.raise_for_status()
    return [m["id"] for m in response.json().get("data", []) if m.get("id")]


def _list_openai_models(provider: Dict[str, Any]) -> List[str]:
    headers = {}
    key = api_key(provider["id"])
    if key:
        headers["Authorization"] = f"Bearer {key}"
    elif provider["requires_key"]:
        raise ProviderError("no API key configured")
    response = requests.get(
        f"{provider['base_url'].rstrip('/')}/models", headers=headers, timeout=15
    )
    response.raise_for_status()
    return [m["id"] for m in response.json().get("data", []) if m.get("id")]


def test_provider(provider_id: str) -> Dict[str, Any]:
    """Probe a provider so the user finds out about a bad key here, not mid-chat."""
    provider = require_provider(provider_id)
    if provider["requires_key"] and not api_key(provider_id):
        return {"provider": provider_id, "ok": False, "error": "no API key configured"}
    if provider["kind"] == KIND_OLLAMA:
        from .ollama_client import OllamaClient

        ok = OllamaClient().is_available()
        return {"provider": provider_id, "ok": ok,
                "error": "" if ok else "Ollama is not reachable"}
    result = list_models(provider_id)
    if result.get("error"):
        return {"provider": provider_id, "ok": False, "error": result["error"]}
    return {"provider": provider_id, "ok": True, "models": len(result["models"])}
