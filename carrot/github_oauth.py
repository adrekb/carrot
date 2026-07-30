"""GitHub OAuth Device Flow + contribution grid.

Uses the OAuth Device Flow (no client secret required) so a desktop app can
authorize against GitHub. The user supplies an OAuth App ``client_id`` in
Settings. All outbound GitHub calls are isolated in small functions so tests
can monkeypatch them.
"""

from __future__ import annotations

import asyncio
import json
import webbrowser
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from .config import get_config, set_config
from .database import get_db

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
GRAPHQL_URL = "https://api.github.com/graphql"
SCOPE = "read:user"

GRID_QUERY = """
query {
  viewer {
    login
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            date
            contributionCount
            level
          }
        }
      }
    }
  }
}
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Config helpers ---------------------------------------------------------

def _cfg(key: str, default: str = "") -> str:
    try:
        return get_config().get(key, default) or default
    except Exception:
        return default


def client_id() -> str:
    return str(_cfg("github_client_id")).strip()


def _token() -> str:
    return _cfg("github_token")


def _set_state(state: str) -> None:
    set_config("github_auth_state", state)


# --- Outbound GitHub calls (monkeypatchable) --------------------------------

def request_device_code(cid: str) -> Dict[str, Any]:
    """POST to GitHub to start the device flow. Returns device-code payload."""
    resp = httpx.post(
        DEVICE_CODE_URL,
        data={"client_id": cid, "scope": SCOPE},
        headers={"Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def exchange_device_code(cid: str, device_code: str) -> Dict[str, Any]:
    """Poll the access-token endpoint once. Returns the JSON response."""
    resp = httpx.post(
        ACCESS_TOKEN_URL,
        data={
            "client_id": cid,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
        headers={"Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_grid(token: str) -> Dict[str, Any]:
    """Query the GraphQL contributions calendar. Returns parsed ``data``."""
    resp = httpx.post(
        GRAPHQL_URL,
        json={"query": GRID_QUERY},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body and "data" not in body:
        raise RuntimeError(body["errors"])
    return body.get("data", {})


# --- Cache ------------------------------------------------------------------

def _cache_grid(data: Dict[str, Any]) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO github_cache (key, data, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
        ("grid", json.dumps(data), _now()),
    )
    conn.commit()


def get_grid() -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        "SELECT data, updated_at FROM github_cache WHERE key = 'grid'"
    ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["data"] or "{}")
    except (ValueError, TypeError):
        data = {}
    return {"data": data, "updated_at": row["updated_at"]}


def fetch_and_cache_grid(token: str) -> Dict[str, Any]:
    data = fetch_grid(token)
    _cache_grid(data)
    login = ""
    try:
        login = data["viewer"]["login"]
    except (KeyError, TypeError):
        pass
    if login:
        set_config("github_login", login)
    return data


# --- Auth orchestration -----------------------------------------------------

def start_auth() -> Dict[str, Any]:
    """Begin device flow. Raises ValueError if no client id is configured."""
    cid = client_id()
    if not cid:
        raise ValueError("Configure a GitHub OAuth App Client ID in Settings first.")

    payload = request_device_code(cid)
    device_code = payload.get("device_code", "")
    user_code = payload.get("user_code", "")
    verification_uri = payload.get("verification_uri", "https://github.com/login/device")
    interval = int(payload.get("interval", 5))

    set_config("github_pending_code", device_code)
    _set_state("pending")

    try:
        webbrowser.open(f"{verification_uri}?user_code={user_code}")
    except Exception:
        pass

    try:
        asyncio.create_task(poll_for_token(cid, device_code, interval))
    except RuntimeError:
        # No running event loop (e.g. called outside async context); skip polling.
        pass

    return {
        "user_code": user_code,
        "verification_uri": verification_uri,
        "interval": interval,
    }


async def poll_for_token(cid: str, device_code: str, interval: int) -> None:
    """Poll GitHub until the user authorizes (or an error/timeout occurs)."""
    wait = max(1, interval)
    attempts = 0
    max_attempts = 180  # ~15 min at 5s
    while attempts < max_attempts:
        attempts += 1
        try:
            body = exchange_device_code(cid, device_code)
        except Exception:
            await asyncio.sleep(wait)
            continue

        if body.get("access_token"):
            token = body["access_token"]
            set_config("github_token", token)
            try:
                fetch_and_cache_grid(token)
                _set_state("authorized")
            except Exception:
                _set_state("authorized")  # token ok; grid can refresh later
            set_config("github_pending_code", "")
            return

        error = body.get("error", "")
        if error in ("authorization_pending", "slow_down"):
            if error == "slow_down":
                wait += 5
            await asyncio.sleep(wait)
            continue

        # access_denied, expired_token, etc.
        _set_state("error")
        set_config("github_pending_code", "")
        return

    _set_state("error")


def auth_status() -> Dict[str, Any]:
    cid = client_id()
    return {
        "configured": bool(cid),
        "state": _cfg("github_auth_state", "none"),
        "login": _cfg("github_login"),
        "has_token": bool(_token()),
    }


def refresh_grid() -> bool:
    """Re-fetch the grid using the stored token. Returns success."""
    token = _token()
    if not token:
        return False
    try:
        fetch_and_cache_grid(token)
        return True
    except Exception:
        return False


def disconnect() -> None:
    """Clear token, auth state, login and cached grid."""
    set_config("github_token", "")
    set_config("github_auth_state", "none")
    set_config("github_login", "")
    set_config("github_pending_code", "")
    conn = get_db()
    conn.execute("DELETE FROM github_cache WHERE key = 'grid'")
    conn.commit()
