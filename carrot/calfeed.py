"""Google Calendar (and any other calendar) via the secret iCal URL.

No Developer Console, no OAuth screens, no API keys, completely free: every
Google Calendar has a permanent private "Secret address in iCal format" URL
(Settings -> Settings for my calendars -> pick a calendar -> scroll down).
Anything that serves an .ics file works the same way — Outlook, Proton,
Fastmail, university timetables.

The URL is a secret (anyone holding it can read the calendar), so it is
stored in config under a redacted key and never returned by the API once
saved. The feed is fetched at most every 15 minutes and cached on disk, so
the dashboard widget and the chat context stay fast and work offline.

Two independent toggles:
  - ``calendar_enabled``: fetch the feed at all (drives the widget).
  - ``calendar_agent_aware``: additionally let the chat assistant see the
    next few days of events as context, so "what does my day look like"
    just works. Off by default until the user opts in.
"""
import os
import re
import json
from datetime import datetime, date, time, timedelta, timezone
from typing import Optional

import requests

from carrot.config import CARROT_DIR, get_config, set_config

CACHE_PATH = os.path.join(CARROT_DIR, "config", "calendar_feed.json")
CACHE_MINUTES = 15
FETCH_TIMEOUT = 15
MAX_EXPANSIONS = 500  # hard cap when expanding a recurring event
_fail_until: Optional[datetime] = None

_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


# ===== ICS parsing (no dependencies) =====

def _unfold(text: str) -> list:
    """RFC 5545 line unfolding: a line starting with space/tab continues
    the previous one."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return (value.replace("\\n", " ").replace("\\N", " ")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _parse_dt(value: str, params: str) -> Optional[dict]:
    """Parse a DTSTART/DTEND value into {'dt': datetime, 'all_day': bool}.

    Handles the three shapes Google emits: 20260801 (all-day),
    20260801T100000Z (UTC), 20260801T100000 with or without a TZID
    parameter (treated as local wall-clock time — good enough for
    "what's on my calendar today").
    """
    value = value.strip()
    try:
        if re.fullmatch(r"\d{8}", value):
            d = datetime.strptime(value, "%Y%m%d")
            return {"dt": d, "all_day": True}
        if value.endswith("Z"):
            d = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return {"dt": d.astimezone().replace(tzinfo=None), "all_day": False}
        d = datetime.strptime(value, "%Y%m%dT%H%M%S")
        return {"dt": d, "all_day": False}
    except ValueError:
        return None


def parse_ics(text: str) -> list:
    """Parse VEVENT blocks into raw event dicts (before recurrence expansion)."""
    events = []
    current = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None and current.get("start"):
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, _, params = head.partition(";")
        name = name.upper()
        if name == "SUMMARY":
            current["title"] = _unescape(value).strip()
        elif name == "LOCATION":
            current["location"] = _unescape(value).strip()
        elif name == "DTSTART":
            current["start"] = _parse_dt(value, params)
        elif name == "DTEND":
            current["end"] = _parse_dt(value, params)
        elif name == "RRULE":
            current["rrule"] = value.strip()
        elif name == "EXDATE":
            exdates = current.setdefault("exdates", set())
            for part in value.split(","):
                parsed = _parse_dt(part, params)
                if parsed:
                    exdates.add(parsed["dt"].date())
    return events


def _parse_rrule(rule: str) -> dict:
    out = {}
    for part in rule.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.upper()] = v
    return out


def _expand(event: dict, window_start: datetime, window_end: datetime) -> list:
    """Expand one event (recurring or not) into occurrences inside the window."""
    start = event["start"]["dt"]
    all_day = event["start"]["all_day"]
    end_info = event.get("end")
    duration = (end_info["dt"] - start) if end_info else (timedelta(days=1) if all_day else timedelta(hours=1))
    exdates = event.get("exdates", set())

    def occurrence(s):
        return {
            "title": event.get("title", "(untitled)"),
            "location": event.get("location", ""),
            "start": s.isoformat(),
            "end": (s + duration).isoformat(),
            "all_day": all_day,
        }

    rule = event.get("rrule")
    if not rule:
        if window_start <= start + duration and start <= window_end and start.date() not in exdates:
            return [occurrence(start)]
        return []

    parsed = _parse_rrule(rule)
    freq = parsed.get("FREQ", "").upper()
    interval = max(int(parsed.get("INTERVAL", 1) or 1), 1)
    count = int(parsed["COUNT"]) if parsed.get("COUNT", "").isdigit() else None
    until = None
    if parsed.get("UNTIL"):
        u = _parse_dt(parsed["UNTIL"], "")
        until = u["dt"] if u else None

    step = {"DAILY": timedelta(days=interval),
            "WEEKLY": timedelta(weeks=interval)}.get(freq)
    bydays = [_WEEKDAYS[d] for d in parsed.get("BYDAY", "").split(",") if d in _WEEKDAYS]

    out = []
    emitted = 0
    cursor = start
    for _ in range(MAX_EXPANSIONS):
        if until and cursor > until:
            break
        if count is not None and emitted >= count:
            break
        candidates = [cursor]
        if freq == "WEEKLY" and bydays:
            week_start = cursor - timedelta(days=cursor.weekday())
            candidates = [week_start + timedelta(days=d) for d in sorted(bydays)]
        for c in candidates:
            if c < start or (until and c > until):
                continue
            if count is not None and emitted >= count:
                break
            emitted += 1
            if window_start <= c + duration and c <= window_end and c.date() not in exdates:
                out.append(occurrence(c))
        if cursor > window_end:
            break
        if step:
            cursor += step
        elif freq == "MONTHLY":
            month = cursor.month - 1 + interval
            year = cursor.year + month // 12
            month = month % 12 + 1
            try:
                cursor = cursor.replace(year=year, month=month)
            except ValueError:  # e.g. Jan 31 -> Feb
                break
        elif freq == "YEARLY":
            try:
                cursor = cursor.replace(year=cursor.year + interval)
            except ValueError:  # Feb 29
                break
        else:
            break
    return out


# ===== Fetch + cache =====

def _load_cache() -> Optional[dict]:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def fetch_ics(force: bool = False) -> Optional[str]:
    """The raw ICS text: cached for 15 minutes, stale-served when offline."""
    global _fail_until
    url = get_config().get("calendar_ics_url", "")
    if not url:
        return None
    cache = _load_cache()
    if cache and not force:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cache["fetched_at"])
            if age < timedelta(minutes=CACHE_MINUTES):
                return cache["ics"]
        except (KeyError, ValueError):
            pass
    if _fail_until and datetime.now(timezone.utc) < _fail_until and not force:
        return cache["ics"] if cache else None
    try:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("calendar URL must be http(s)")
        resp = requests.get(url, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        text = resp.text
        if "BEGIN:VCALENDAR" not in text[:2000]:
            raise ValueError("not an iCal feed")
        _fail_until = None
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(), "ics": text}, f)
        return text
    except Exception:
        _fail_until = datetime.now(timezone.utc) + timedelta(minutes=CACHE_MINUTES)
        return cache["ics"] if cache else None


def upcoming_events(days: int = 14, force: bool = False) -> Optional[list]:
    """Events in [today 00:00, today + days], sorted. None = no URL / unreachable."""
    text = fetch_ics(force=force)
    if text is None:
        return None
    window_start = datetime.combine(date.today(), time.min)
    window_end = window_start + timedelta(days=days)
    out = []
    for event in parse_ics(text):
        out.extend(_expand(event, window_start, window_end))
    out.sort(key=lambda e: e["start"])
    return out


# ===== Status + agent context =====

def status() -> dict:
    cfg = get_config()
    url = cfg.get("calendar_ics_url", "")
    cache = _load_cache()
    return {
        "enabled": bool(cfg.get("calendar_enabled", False)),
        "agent_aware": bool(cfg.get("calendar_agent_aware", False)),
        # Never echo the secret URL — just enough to recognize it.
        "url_set": bool(url),
        "url_hint": ("…" + url[-12:]) if url else "",
        "last_fetched": cache.get("fetched_at") if cache else None,
    }


def agent_context(days: int = 7, max_events: int = 15) -> str:
    """A compact system-prompt block of upcoming events, or "" when the
    calendar is off, not agent-aware, not configured, or unreachable."""
    cfg = get_config()
    if not cfg.get("calendar_enabled", False) or not cfg.get("calendar_agent_aware", False):
        return ""
    events = upcoming_events(days=days)
    if events is None:
        return ""
    now = datetime.now()
    lines = [f"Today is {now.strftime('%A, %B %d, %Y')} and the local time is {now.strftime('%H:%M')}.",
             f"The user's calendar for the next {days} days:"]
    if not events:
        lines.append("- (no events)")
    for e in events[:max_events]:
        start = datetime.fromisoformat(e["start"])
        when = start.strftime("%a %b %d") if e["all_day"] else start.strftime("%a %b %d %H:%M")
        loc = f" @ {e['location']}" if e.get("location") else ""
        lines.append(f"- {when}: {e['title']}{loc}")
    if len(events) > max_events:
        lines.append(f"- (+{len(events) - max_events} more)")
    return "\n".join(lines)
