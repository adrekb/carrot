"""Tests for the secret-iCal calendar add-on (carrot/calfeed.py)."""
from datetime import datetime, date, timedelta

from carrot import calfeed, config


def _dt(days_ahead, hour=10):
    d = datetime.combine(date.today() + timedelta(days=days_ahead), datetime.min.time())
    return d.replace(hour=hour)


def _stamp(dt):
    return dt.strftime("%Y%m%dT%H%M%S")


def _ics(body):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + body + "END:VCALENDAR\r\n"


# ===== Parsing =====

def test_parse_basic_timed_event():
    start, end = _dt(1), _dt(1, 11)
    text = _ics(
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{_stamp(start)}\r\n"
        f"DTEND:{_stamp(end)}\r\n"
        "SUMMARY:Team standup\\, weekly\r\n"
        "LOCATION:Room 4\r\n"
        "END:VEVENT\r\n"
    )
    events = calfeed.parse_ics(text)
    assert len(events) == 1
    assert events[0]["title"] == "Team standup, weekly"  # escaped comma unescaped
    assert events[0]["location"] == "Room 4"
    assert events[0]["start"]["all_day"] is False


def test_parse_all_day_and_folded_line():
    day = (date.today() + timedelta(days=2)).strftime("%Y%m%d")
    text = _ics(
        "BEGIN:VEVENT\r\n"
        f"DTSTART;VALUE=DATE:{day}\r\n"
        "SUMMARY:A very long title that got \r\n"
        " folded across two lines\r\n"
        "END:VEVENT\r\n"
    )
    events = calfeed.parse_ics(text)
    assert events[0]["title"] == "A very long title that got folded across two lines"
    assert events[0]["start"]["all_day"] is True


def test_weekly_recurrence_with_until_and_exdate():
    start = _dt(-7, 9)  # started last week, recurs weekly
    until = start + timedelta(days=30)
    skipped = (start + timedelta(days=7)).date()
    text = _ics(
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{_stamp(start)}\r\n"
        f"DTEND:{_stamp(start + timedelta(hours=1))}\r\n"
        "SUMMARY:Gym\r\n"
        f"RRULE:FREQ=WEEKLY;UNTIL={until.strftime('%Y%m%dT%H%M%S')}\r\n"
        f"EXDATE:{_stamp(start + timedelta(days=7))}\r\n"
        "END:VEVENT\r\n"
    )
    window_start = datetime.combine(date.today(), datetime.min.time())
    occurrences = calfeed._expand(calfeed.parse_ics(text)[0],
                                  window_start, window_start + timedelta(days=14))
    starts = [datetime.fromisoformat(o["start"]).date() for o in occurrences]
    assert starts  # future occurrences generated from a past DTSTART
    assert skipped not in starts  # EXDATE honored
    assert all(window_start.date() <= s for s in starts)


def test_daily_recurrence_respects_count():
    start = _dt(0, 8)
    text = _ics(
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{_stamp(start)}\r\n"
        "SUMMARY:Meds\r\n"
        "RRULE:FREQ=DAILY;COUNT=3\r\n"
        "END:VEVENT\r\n"
    )
    window_start = datetime.combine(date.today(), datetime.min.time())
    occurrences = calfeed._expand(calfeed.parse_ics(text)[0],
                                  window_start, window_start + timedelta(days=30))
    assert len(occurrences) == 3


# ===== upcoming_events + agent context =====

def _install_feed(monkeypatch, text):
    monkeypatch.setattr(calfeed, "fetch_ics", lambda force=False: text)


def test_upcoming_events_sorted_and_windowed(monkeypatch):
    text = _ics(
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{_stamp(_dt(3))}\r\nSUMMARY:Later\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{_stamp(_dt(1))}\r\nSUMMARY:Sooner\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{_stamp(_dt(60))}\r\nSUMMARY:Out of window\r\nEND:VEVENT\r\n"
    )
    _install_feed(monkeypatch, text)
    events = calfeed.upcoming_events(days=14)
    assert [e["title"] for e in events] == ["Sooner", "Later"]


def test_agent_context_respects_both_toggles(isolated_db, monkeypatch):
    _install_feed(monkeypatch, _ics(
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{_stamp(_dt(1, 14))}\r\nSUMMARY:Dentist\r\nLOCATION:Main St\r\nEND:VEVENT\r\n"
    ))
    config.set_config("calendar_ics_url", "https://example.test/basic.ics")

    # Off by default: nothing leaks into the prompt without opt-in.
    config.set_config("calendar_enabled", True)
    config.set_config("calendar_agent_aware", False)
    assert calfeed.agent_context() == ""

    config.set_config("calendar_agent_aware", True)
    block = calfeed.agent_context()
    assert "Dentist" in block and "Main St" in block
    assert "calendar for the next" in block

    config.set_config("calendar_enabled", False)
    assert calfeed.agent_context() == ""


# ===== API =====

def test_calendar_config_never_echoes_secret_url(client):
    secret = "https://calendar.google.com/calendar/ical/u/private-abc123/basic.ics"
    resp = client.put("/api/calendar/config", json={"ics_url": secret, "agent_aware": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["url_set"] is True
    assert secret not in str(data)          # only a short hint is shown
    assert data["url_hint"].startswith("…")
    # /api/config redacts it too.
    cfg = client.get("/api/config").json()
    assert cfg.get("calendar_ics_url") in (True, False)  # boolean, not the URL


def test_calendar_config_rejects_non_http(client):
    resp = client.put("/api/calendar/config", json={"ics_url": "file:///etc/passwd"})
    assert resp.status_code == 400


def test_events_endpoint_when_not_configured(client):
    client.put("/api/calendar/config", json={"ics_url": "", "enabled": False})
    data = client.get("/api/calendar/events").json()
    assert data["configured"] is False and data["events"] == []


def test_chat_history_includes_calendar_when_aware(client, monkeypatch):
    from carrot import app as app_mod
    monkeypatch.setattr(app_mod.calfeed_mod, "agent_context",
                        lambda days=7, max_events=15: "The user's calendar for the next 7 days:\n- Fri: Dentist")
    history, _ = app_mod._prepare_history({"id": "c1"}, "what's my week look like?", None)
    assert any("Dentist" in h["content"] for h in history if h["role"] == "system")
