"""Apple Health sync via inbound Apple Shortcuts webhook.

The user's iPhone runs an iOS Shortcut that POSTs daily activity data to
``POST /api/health/sync``. No HealthKit code lives here; this module only
normalizes and stores whatever the Shortcut sends.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .database import get_db

# Canonical metric names and the aliases an inbound payload may use.
METRIC_ALIASES: Dict[str, List[str]] = {
    "move": ["move", "active_energy", "activeenergy", "calories", "active_calories", "energy"],
    "exercise": ["exercise", "exercise_minutes", "exerciseminutes", "exercise_time"],
    "stand": ["stand", "stand_hours", "standhours", "stand_time"],
}

METRIC_UNITS: Dict[str, str] = {
    "move": "kcal",
    "exercise": "min",
    "stand": "hr",
}

# Reverse lookup: alias -> canonical metric.
_ALIAS_MAP: Dict[str, str] = {}
for _canonical, _aliases in METRIC_ALIASES.items():
    for _a in _aliases:
        _ALIAS_MAP[_a.lower()] = _canonical


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_metric(key: str) -> Optional[str]:
    return _ALIAS_MAP.get(str(key).strip().lower())


def sync_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and upsert an inbound metrics payload.

    Accepts tolerant keys (e.g. ``active_energy``/``move``/``calories``),
    an optional ``date`` (default today), ``unit`` and ``source``.
    Auto-adds the fitness widget on first successful sync.
    """
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object")

    sync_date = str(payload.get("date") or _today())
    source = str(payload.get("source") or "apple_shortcuts")
    default_unit = str(payload.get("unit") or "")

    received = 0
    stored = 0
    conn = get_db()
    now = _now()

    for key, value in payload.items():
        if key in ("date", "source", "unit"):
            continue
        metric = _normalize_metric(key)
        if metric is None:
            continue
        received += 1
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        unit = default_unit or METRIC_UNITS.get(metric, "")
        conn.execute(
            "INSERT INTO health_metrics (date, metric, value, unit, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date, metric) DO UPDATE SET "
            "value = excluded.value, unit = excluded.unit, "
            "source = excluded.source, updated_at = excluded.updated_at",
            (sync_date, metric, num, unit, source, now),
        )
        stored += 1

    conn.commit()

    # Auto-add the fitness widget so rings appear immediately after first sync.
    if stored > 0:
        try:
            from . import widgets

            widgets.ensure_widget("fitness")
        except Exception:
            pass

    return {"received": received, "stored": stored, "date": sync_date}


def today_metrics(target_date: Optional[str] = None) -> Dict[str, Any]:
    """Return ``{metric: {value, unit}}`` for a given date (default today)."""
    target = target_date or _today()
    conn = get_db()
    rows = conn.execute(
        "SELECT metric, value, unit FROM health_metrics WHERE date = ?",
        (target,),
    ).fetchall()
    out: Dict[str, Any] = {}
    for r in rows:
        out[r["metric"]] = {"value": r["value"], "unit": r["unit"]}
    return out


def range_metrics(days: int = 90) -> List[Dict[str, Any]]:
    """Return recent metric rows for history/trends."""
    conn = get_db()
    rows = conn.execute(
        "SELECT date, metric, value, unit FROM health_metrics "
        "ORDER BY date DESC LIMIT ?",
        (max(1, days) * len(METRIC_ALIASES),),
    ).fetchall()
    return [
        {"date": r["date"], "metric": r["metric"], "value": r["value"], "unit": r["unit"]}
        for r in rows
    ]
