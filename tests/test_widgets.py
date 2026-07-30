"""Tests for the add-on widget system and Apple Health sync."""
import pytest

from carrot import widgets, health


# ===== Widget catalog & store =====

def test_catalog_lists_builtin_widgets(client):
    resp = client.get("/api/widgets/catalog")
    assert resp.status_code == 200
    catalog = resp.json()["catalog"]
    types = {item["type"] for item in catalog}
    assert {"fitness", "github"} <= types
    # Nothing installed yet.
    assert all(item["installed"] is False for item in catalog)


def test_add_list_remove_widget(client):
    resp = client.post("/api/widgets", json={"type": "fitness"})
    assert resp.status_code == 200
    widget = resp.json()
    assert widget["type"] == "fitness"
    widget_id = widget["id"]

    listed = client.get("/api/widgets").json()["widgets"]
    assert any(w["id"] == widget_id for w in listed)

    # Catalog now flags it installed.
    catalog = client.get("/api/widgets/catalog").json()["catalog"]
    fitness = next(c for c in catalog if c["type"] == "fitness")
    assert fitness["installed"] is True

    # Remove it.
    assert client.delete(f"/api/widgets/{widget_id}").status_code == 200
    assert client.get("/api/widgets").json()["widgets"] == []


def test_duplicate_add_rejected(client):
    assert client.post("/api/widgets", json={"type": "github"}).status_code == 200
    dup = client.post("/api/widgets", json={"type": "github"})
    assert dup.status_code == 400


def test_unknown_widget_rejected(client):
    assert client.post("/api/widgets", json={"type": "nope"}).status_code == 400


def test_update_widget_config_merges(client):
    widget = client.post("/api/widgets", json={"type": "fitness"}).json()
    resp = client.put(
        f"/api/widgets/{widget['id']}",
        json={"config": {"move_goal": 750}},
    )
    assert resp.status_code == 200
    cfg = resp.json()["config"]
    assert cfg["move_goal"] == 750
    # Default keys preserved.
    assert cfg["exercise_goal"] == 40


# ===== Apple Health sync =====

def test_health_sync_upserts_and_autocreate_widget(client):
    payload = {"move": 420, "exercise_minutes": 35, "stand_hours": 10}
    resp = client.post("/api/health/sync", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["stored"] == 3

    # Fitness widget auto-created on first sync.
    types = {w["type"] for w in client.get("/api/widgets").json()["widgets"]}
    assert "fitness" in types

    today = client.get("/api/health/today").json()["metrics"]
    assert today["move"]["value"] == 420
    assert today["exercise"]["value"] == 35
    assert today["stand"]["value"] == 10


def test_health_sync_alias_keys(client):
    resp = client.post(
        "/api/health/sync",
        json={"activeEnergy": 500, "exerciseMinutes": 20, "standHours": 8},
    )
    assert resp.status_code == 200
    today = client.get("/api/health/today").json()["metrics"]
    assert today["move"]["value"] == 500
    assert today["exercise"]["value"] == 20
    assert today["stand"]["value"] == 8


def test_health_sync_idempotent_same_day(client):
    client.post("/api/health/sync", json={"move": 100})
    client.post("/api/health/sync", json={"move": 250})
    today = client.get("/api/health/today").json()["metrics"]
    # Upsert overwrites rather than duplicating.
    assert today["move"]["value"] == 250


def test_health_sync_rejects_non_object(client):
    # A non-object body is rejected (FastAPI validates the dict body as 422).
    assert client.post("/api/health/sync", json=[1, 2, 3]).status_code in (400, 422)


def test_health_range(client):
    client.post("/api/health/sync", json={"move": 100, "date": "2026-01-01"})
    client.post("/api/health/sync", json={"move": 200, "date": "2026-01-02"})
    resp = client.get("/api/health/range?days=30")
    assert resp.status_code == 200
    rows = resp.json()["metrics"]
    assert len(rows) == 2
