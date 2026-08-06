"""Two ways to be signed in: a developer API key, or a consumer subscription.

Most people who use Carrot with Anthropic or OpenAI are already paying one of
them every month — for Claude Pro/Max or ChatGPT Plus. Being told to go create
a *second*, separately-billed developer account before the app will talk to the
same model is a genuinely bad first five minutes, and it is the reason a lot of
local-first tools never get used past their first run.

So each provider carries an **auth mode**:

* ``api_key`` — a developer key from the console. Billed per token. This is
  what Carrot has always done and it stays the default.
* ``subscription`` — sign in with the account you already pay for, through the
  provider's own OAuth flow, and use your plan's allowance.

**The line this module does not cross.** "Use my web subscription" is
implemented as OAuth against the provider's published authorization endpoint —
the same sanctioned flow their own first-party CLIs use, with a token they
issued to a client they registered. It is *not* implemented by lifting session
cookies out of a browser profile, replaying a `sessionKey`, or driving the web
UI headlessly. Those work for a while, break constantly, and violate the terms
of both services. If a provider has no OAuth flow configured, this module says
so plainly rather than reaching for the browser jar.

Because OAuth client registration is per-installation, the client id and
endpoints are configuration rather than constants baked in here. Carrot ships
the shape of the flow, not someone else's credentials.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

import requests

from .config import get_config, set_config

MODE_API_KEY = "api_key"
MODE_SUBSCRIPTION = "subscription"
MODES = (MODE_API_KEY, MODE_SUBSCRIPTION)

# Refresh this far before the token actually dies, so a long streaming request
# does not expire halfway through.
REFRESH_MARGIN_SECONDS = 120
TOKEN_TIMEOUT = 30

# Where a login lands. Loopback is the only redirect a desktop app can own, and
# the port is the one the backend already listens on.
DEFAULT_REDIRECT = "http://127.0.0.1:8181/api/auth/callback"

# Providers that have a consumer plan worth signing in with. `client_id`,
# `authorize_url` and `token_url` are filled from config: an installation
# registers its own OAuth client, and Carrot does not ship one.
SUBSCRIPTION_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic",
        "plan": "Claude Pro or Max",
        "scopes": "user:inference",
        "header": "authorization_bearer",
        "docs": "https://claude.ai/settings",
    },
    "openai": {
        "label": "OpenAI",
        "plan": "ChatGPT Plus or Pro",
        "scopes": "openid profile email",
        "header": "authorization_bearer",
        "docs": "https://chatgpt.com",
    },
}


class AuthError(RuntimeError):
    """Something about signing in failed, phrased for the person reading it."""


# ===== Mode =====

def mode(provider_id: str) -> str:
    """Which credential this provider uses. Unknown values mean the key path.

    Falling back to ``api_key`` is deliberate: an unrecognized mode should
    degrade to the thing that has always worked, not to the thing that needs a
    browser round trip.
    """
    stored = (get_config().get("auth_modes", {}) or {}).get(provider_id, "")
    return stored if stored in MODES else MODE_API_KEY


def set_mode(provider_id: str, value: str) -> str:
    if value not in MODES:
        raise AuthError(f"unknown auth mode: {value}")
    if value == MODE_SUBSCRIPTION and provider_id not in SUBSCRIPTION_PROVIDERS:
        raise AuthError(f"{provider_id} has no consumer subscription to sign in with")
    modes = dict(get_config().get("auth_modes", {}) or {})
    modes[provider_id] = value
    set_config("auth_modes", modes)
    return value


# ===== OAuth client configuration =====

def oauth_config(provider_id: str) -> Dict[str, str]:
    """This installation's OAuth client for a provider, if it has one."""
    if provider_id not in SUBSCRIPTION_PROVIDERS:
        raise AuthError(f"{provider_id} does not support subscription sign-in")
    stored = (get_config().get("oauth_clients", {}) or {}).get(provider_id, {})
    spec = SUBSCRIPTION_PROVIDERS[provider_id]
    return {
        "client_id": stored.get("client_id", ""),
        "authorize_url": stored.get("authorize_url", ""),
        "token_url": stored.get("token_url", ""),
        "scopes": stored.get("scopes", spec["scopes"]),
        "redirect_uri": stored.get("redirect_uri", DEFAULT_REDIRECT),
    }


def set_oauth_config(provider_id: str, **fields: str) -> Dict[str, str]:
    if provider_id not in SUBSCRIPTION_PROVIDERS:
        raise AuthError(f"{provider_id} does not support subscription sign-in")
    for name in ("authorize_url", "token_url", "redirect_uri"):
        value = fields.get(name)
        if value and not re.match(r"^https://|^http://127\.0\.0\.1|^http://localhost", value):
            # An http endpoint on the open internet would put the token on the
            # wire in clear text; loopback is the one safe exception.
            raise AuthError(f"{name} must be https (or loopback)")
    clients = dict(get_config().get("oauth_clients", {}) or {})
    current = dict(clients.get(provider_id, {}))
    current.update({k: v for k, v in fields.items() if v is not None})
    clients[provider_id] = current
    set_config("oauth_clients", clients)
    return oauth_config(provider_id)


def _require_oauth(provider_id: str) -> Dict[str, str]:
    config = oauth_config(provider_id)
    missing = [k for k in ("client_id", "authorize_url", "token_url") if not config[k]]
    if missing:
        spec = SUBSCRIPTION_PROVIDERS[provider_id]
        raise AuthError(
            f"signing in with your {spec['plan']} subscription needs this "
            f"installation's OAuth client details ({', '.join(missing)}). Add them "
            f"in Settings → Providers → {spec['label']} → Subscription, or use an "
            f"API key instead."
        )
    return config


# ===== The login flow (OAuth 2.0, PKCE) =====

_PENDING: Dict[str, Dict[str, Any]] = {}
PENDING_TTL_SECONDS = 600


def begin_login(provider_id: str) -> Dict[str, str]:
    """Start a sign-in and return the URL to open.

    PKCE, because a desktop app cannot keep a client secret — anything shipped
    to the user's disk is not a secret. The verifier stays in this process and
    never touches the config file.
    """
    config = _require_oauth(provider_id)
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    state = secrets.token_urlsafe(24)

    _sweep_pending()
    _PENDING[state] = {
        "provider": provider_id, "verifier": verifier, "started": time.time(),
    }
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": config["client_id"],
        "redirect_uri": config["redirect_uri"],
        "scope": config["scopes"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return {"url": f"{config['authorize_url']}?{query}", "state": state}


def _sweep_pending() -> None:
    cutoff = time.time() - PENDING_TTL_SECONDS
    for state in [s for s, v in _PENDING.items() if v["started"] < cutoff]:
        _PENDING.pop(state, None)


def complete_login(state: str, code: str) -> Dict[str, Any]:
    """Exchange the authorization code for tokens.

    The state has to match one this process started. Accepting an unknown state
    would let any page that can reach the loopback port complete a login on the
    user's behalf, which is the whole reason state exists.
    """
    _sweep_pending()
    pending = _PENDING.pop(state or "", None)
    if not pending:
        raise AuthError("this sign-in expired or did not start here — try again")
    if not code:
        raise AuthError("the provider did not return an authorization code")

    provider_id = pending["provider"]
    config = _require_oauth(provider_id)
    try:
        response = requests.post(
            config["token_url"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": config["client_id"],
                "redirect_uri": config["redirect_uri"],
                "code_verifier": pending["verifier"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=TOKEN_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AuthError(f"could not reach the sign-in service: {exc}")
    if response.status_code >= 400:
        raise AuthError(_token_error(response))
    return _store_tokens(provider_id, response.json())


def _token_error(response) -> str:
    try:
        payload = response.json()
        detail = payload.get("error_description") or payload.get("error") or ""
    except Exception:
        detail = (response.text or "")[:200]
    return f"sign-in failed ({response.status_code}): {detail}".strip()


# ===== Token storage =====

def _all_tokens() -> Dict[str, Dict[str, Any]]:
    raw = get_config().get("oauth_tokens", {})
    return raw if isinstance(raw, dict) else {}


def _store_tokens(provider_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    access = payload.get("access_token")
    if not access:
        raise AuthError("the provider's response contained no access token")
    expires_in = payload.get("expires_in")
    record = {
        "access_token": access,
        # A refresh token is not always reissued; keeping the old one is what
        # makes a long-lived session survive a refresh that omits it.
        "refresh_token": payload.get("refresh_token") or _all_tokens().get(
            provider_id, {}).get("refresh_token", ""),
        "expires_at": time.time() + float(expires_in) if expires_in else 0,
        "scope": payload.get("scope", ""),
        "obtained_at": time.time(),
    }
    stored = dict(_all_tokens())
    stored[provider_id] = record
    set_config("oauth_tokens", stored)
    return {"provider": provider_id, "expires_at": record["expires_at"], "signed_in": True}


def signed_in(provider_id: str) -> bool:
    """Whether there is a usable session — a live token, or one we can renew."""
    record = _all_tokens().get(provider_id) or {}
    if not record.get("access_token"):
        return False
    if not record.get("expires_at"):
        return True  # no stated expiry: treat as long-lived
    if time.time() < record["expires_at"] - REFRESH_MARGIN_SECONDS:
        return True
    return bool(record.get("refresh_token"))


def sign_out(provider_id: str) -> bool:
    stored = dict(_all_tokens())
    if provider_id not in stored:
        return False
    stored.pop(provider_id)
    set_config("oauth_tokens", stored)
    return True


def access_token(provider_id: str) -> str:
    """A live access token, refreshing first if this one is about to expire."""
    record = _all_tokens().get(provider_id) or {}
    token = record.get("access_token", "")
    if not token:
        raise AuthError(
            f"not signed in to {SUBSCRIPTION_PROVIDERS.get(provider_id, {}).get('label', provider_id)}"
        )
    expires_at = record.get("expires_at") or 0
    if expires_at and time.time() >= expires_at - REFRESH_MARGIN_SECONDS:
        return _refresh(provider_id, record)
    return token


def _refresh(provider_id: str, record: Dict[str, Any]) -> str:
    refresh_token = record.get("refresh_token")
    if not refresh_token:
        raise AuthError("your session expired — sign in again")
    config = _require_oauth(provider_id)
    try:
        response = requests.post(
            config["token_url"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": config["client_id"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=TOKEN_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AuthError(f"could not refresh your session: {exc}")
    if response.status_code >= 400:
        # A refused refresh is a dead session; clearing it means the UI shows
        # "sign in" instead of failing every call with the same stale token.
        sign_out(provider_id)
        raise AuthError("your session expired — sign in again")
    _store_tokens(provider_id, response.json())
    return _all_tokens()[provider_id]["access_token"]


# ===== What the HTTP clients actually need =====

def credential(provider_id: str) -> Tuple[str, str]:
    """``(scheme, value)`` for this provider, honouring its mode.

    ``scheme`` is ``api_key`` or ``bearer``. Callers branch on it rather than
    guessing from the value, because the two go in different headers and an
    OAuth token sent as ``x-api-key`` fails in a way nobody can debug.
    """
    from . import providers as providers_mod

    if mode(provider_id) == MODE_SUBSCRIPTION:
        return "bearer", access_token(provider_id)
    return "api_key", providers_mod.api_key(provider_id)


def has_credential(provider_id: str) -> bool:
    """Whether this provider can be called at all right now, either way."""
    from . import providers as providers_mod

    if mode(provider_id) == MODE_SUBSCRIPTION:
        return signed_in(provider_id)
    return bool(providers_mod.api_key(provider_id))


def status(provider_id: str) -> Dict[str, Any]:
    """Everything the settings panel needs about one provider's auth."""
    from . import providers as providers_mod

    supported = provider_id in SUBSCRIPTION_PROVIDERS
    spec = SUBSCRIPTION_PROVIDERS.get(provider_id, {})
    config = oauth_config(provider_id) if supported else {}
    record = _all_tokens().get(provider_id) or {}
    return {
        "provider": provider_id,
        "mode": mode(provider_id),
        "modes": list(MODES),
        "subscription_supported": supported,
        "plan_label": spec.get("plan", ""),
        "key_set": bool(providers_mod.api_key(provider_id)),
        "signed_in": signed_in(provider_id) if supported else False,
        "expires_at": record.get("expires_at", 0),
        # Never the client id's value in a list view — but whether it is set
        # is exactly what the panel needs to decide what to show.
        "oauth_configured": bool(
            config.get("client_id") and config.get("authorize_url") and config.get("token_url")
        ) if supported else False,
        "usable": has_credential(provider_id),
    }


def all_status() -> Dict[str, Any]:
    from . import providers as providers_mod

    ids = {p["id"] for p in providers_mod.list_providers()} | set(SUBSCRIPTION_PROVIDERS)
    ids.discard(providers_mod.LOCAL_PROVIDER)
    return {"providers": [status(p) for p in sorted(ids)]}
