"""Add-on widget registry and store.

Widgets are user-added dashboard/rail components. The vanilla dashboard starts
empty; users add widgets from a catalog. Each widget instance persists in the
``widgets`` table with a JSON ``config`` blob for per-instance settings.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .database import get_db

# Ordered catalog of available add-on widgets.
WIDGET_CATALOG: List[Dict[str, Any]] = [
    {
        "type": "fitness",
        "name": "Fitness",
        "desc": "Move / Exercise / Stand rings synced from Apple Health.",
        "slot": "rail",
        "needs_setup": False,
    },
    {
        "type": "github",
        "name": "Code Contributions",
        "name_short": "GitHub",
        "desc": "GitHub contribution heatmap via OAuth device flow.",
        "slot": "rail",
        "needs_setup": True,
    },
    {
        "type": "calendar",
        "name": "Calendar",
        "desc": "Month view with your reminders — and your Google Calendar, connected in Settings with one paste.",
        "slot": "canvas",
        "needs_setup": False,
    },
    {
        "type": "weather",
        "name": "Weather",
        "desc": "Current conditions and 5-day forecast via Open-Meteo (no key needed).",
        "slot": "rail",
        "needs_setup": False,
    },
    {
        "type": "quicknotes",
        "name": "Quick Notes",
        "name_short": "Notes",
        "desc": "A dashboard scratchpad that autosaves to your notes.",
        "slot": "rail",
        "needs_setup": False,
    },
]

# Default per-instance config for each widget type.
DEFAULT_CONFIG: Dict[str, Dict[str, Any]] = {
    "fitness": {"move_goal": 600, "exercise_goal": 40, "stand_goal": 12},
    "github": {"login": "", "total": 0},
    "calendar": {},
    "weather": {"lat": None, "lon": None, "label": "", "units": "c"},
    "quicknotes": {"note_id": ""},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_catalog() -> List[Dict[str, Any]]:
    """Return the add-on catalog with an ``installed`` flag per type."""
    installed = {w["type"] for w in list_widgets()}
    out = []
    for item in WIDGET_CATALOG:
        entry = dict(item)
        entry["installed"] = item["type"] in installed
        out.append(entry)
    return out


def _row_to_widget(row: Any) -> Dict[str, Any]:
    try:
        config = json.loads(row["config"] or "{}")
    except (ValueError, TypeError):
        config = {}
    return {
        "id": row["id"],
        "type": row["type"],
        "slot": row["slot"],
        "position": row["position"],
        "config": config,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_widgets(slot: Optional[str] = None) -> List[Dict[str, Any]]:
    """List installed widgets, optionally filtered by slot, ordered by position."""
    conn = get_db()
    if slot:
        rows = conn.execute(
            "SELECT * FROM widgets WHERE slot = ? ORDER BY position, created_at",
            (slot,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM widgets ORDER BY position, created_at"
        ).fetchall()
    return [_row_to_widget(r) for r in rows]


def get_widget(widget_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM widgets WHERE id = ?", (widget_id,)).fetchone()
    return _row_to_widget(row) if row else None


def get_widget_by_type(widget_type: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM widgets WHERE type = ? LIMIT 1", (widget_type,)
    ).fetchone()
    return _row_to_widget(row) if row else None


def add_widget(widget_type: str, slot: Optional[str] = None) -> Dict[str, Any]:
    """Add a widget instance. Rejects unknown or duplicate types."""
    catalog_entry = next(
        (c for c in WIDGET_CATALOG if c["type"] == widget_type), None
    )
    if catalog_entry is None:
        raise ValueError(f"Unknown widget type: {widget_type}")
    if get_widget_by_type(widget_type) is not None:
        raise ValueError(f"Widget already added: {widget_type}")

    target_slot = slot or catalog_entry.get("slot", "rail")
    conn = get_db()
    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) AS m FROM widgets WHERE slot = ?",
        (target_slot,),
    ).fetchone()["m"]

    widget_id = uuid.uuid4().hex[:12]
    now = _now()
    config = dict(DEFAULT_CONFIG.get(widget_type, {}))
    conn.execute(
        "INSERT INTO widgets (id, type, slot, position, config, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            widget_id,
            widget_type,
            target_slot,
            max_pos + 1,
            json.dumps(config),
            now,
            now,
        ),
    )
    conn.commit()
    return get_widget(widget_id)  # type: ignore[return-value]


def ensure_widget(widget_type: str) -> Dict[str, Any]:
    """Return the widget of this type, creating it if absent (idempotent)."""
    existing = get_widget_by_type(widget_type)
    if existing:
        return existing
    return add_widget(widget_type)


def remove_widget(widget_id: str) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM widgets WHERE id = ?", (widget_id,))
    conn.commit()
    return cur.rowcount > 0


def update_widget(widget_id: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Merge ``config`` into the widget's stored config."""
    widget = get_widget(widget_id)
    if widget is None:
        return None
    merged = {**widget["config"], **config}
    conn = get_db()
    conn.execute(
        "UPDATE widgets SET config = ?, updated_at = ? WHERE id = ?",
        (json.dumps(merged), _now(), widget_id),
    )
    conn.commit()
    return get_widget(widget_id)


def set_position(widget_id: str, position: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    conn.execute(
        "UPDATE widgets SET position = ?, updated_at = ? WHERE id = ?",
        (position, _now(), widget_id),
    )
    conn.commit()
    return get_widget(widget_id)
