"""Tests for the GitHub contribution widget (OAuth device flow).

Outbound GitHub HTTP calls are isolated in ``carrot.github_oauth`` functions
(``request_device_code``, ``exchange_device_code``, ``fetch_grid``); these tests
monkeypatch them so no network access occurs.
"""
import asyncio

import pytest

from carrot import github_oauth as gh
from carrot import config


SAMPLE_GRID = {
    "viewer": {
        "login": "krish",
        "contributionsCollection": {
            "contributionCalendar": {
                "totalContributions": 123,
                "weeks": [
                    {
                        "contributionDays": [
                            {"date": "2026-01-01", "contributionCount": 3, "level": 2}
                        ]
                    }
                ],
            }
        },
    }
}


@pytest.fixture
def no_browser(monkeypatch):
    monkeypatch.setattr(gh.webbrowser, "open", lambda *a, **k: None)


def test_missing_client_id_returns_400(client):
    resp = client.post("/api/widgets/github/auth")
    assert resp.status_code == 400
    assert "Client ID" in resp.json()["detail"]


def test_status_unconfigured_by_default(client):
    status = client.get("/api/widgets/github/status").json()
    assert status["configured"] is False
    assert status["state"] == "none"


def test_start_auth_returns_device_code(client, monkeypatch, no_browser):
    client.put("/api/config/github_client_id", json="test-client-id")
    monkeypatch.setattr(
        gh,
        "request_device_code",
        lambda cid: {
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
        },
    )
    # Prevent a real background polling task during the test.
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(gh, "poll_for_token", _noop)

    resp = client.post("/api/widgets/github/auth")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_code"] == "ABCD-1234"
    assert body["verification_uri"] == "https://github.com/login/device"

    status = client.get("/api/widgets/github/status").json()
    assert status["configured"] is True
    assert status["state"] == "pending"


def test_poll_grants_token_and_caches_grid(isolated_db, monkeypatch):
    config.set_config("github_client_id", "test-client-id")
    monkeypatch.setattr(
        gh, "exchange_device_code",
        lambda cid, dc: {"access_token": "tok-xyz", "token_type": "bearer"},
    )
    monkeypatch.setattr(gh, "fetch_grid", lambda token: SAMPLE_GRID)

    asyncio.run(gh.poll_for_token("test-client-id", "dev123", 1))

    status = gh.auth_status()
    assert status["state"] == "authorized"
    assert status["login"] == "krish"
    assert status["has_token"] is True

    grid = gh.get_grid()
    assert grid is not None
    cal = grid["data"]["viewer"]["contributionsCollection"]["contributionCalendar"]
    assert cal["totalContributions"] == 123


def test_disconnect_clears_state(isolated_db, monkeypatch):
    config.set_config("github_token", "tok-xyz")
    config.set_config("github_auth_state", "authorized")
    config.set_config("github_login", "krish")
    monkeypatch.setattr(gh, "fetch_grid", lambda token: SAMPLE_GRID)
    gh.fetch_and_cache_grid("tok-xyz")
    assert gh.get_grid() is not None

    gh.disconnect()

    status = gh.auth_status()
    assert status["state"] == "none"
    assert status["login"] == ""
    assert status["has_token"] is False
    assert gh.get_grid() is None


def test_grid_endpoint_unavailable_when_empty(client):
    resp = client.get("/api/widgets/github/grid")
    assert resp.status_code == 200
    assert resp.json()["available"] is False
