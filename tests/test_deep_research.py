"""Tests for the deep-research pipeline and its overnight scheduler."""
from datetime import datetime


def _parse_sse(text):
    import json
    payloads = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data:"):
            payloads.append(json.loads(frame[len("data:"):].strip()))
    return payloads


def test_deep_research_full_pipeline(client, monkeypatch, fake_ollama, tmp_path):
    """Analyzer -> researcher -> synthesizer with mocked search/fetch/RSS."""
    from carrot import recap as recap_mod
    from carrot import deep_research as dr_mod

    monkeypatch.setattr(dr_mod, "OllamaClient", fake_ollama)
    monkeypatch.setattr(dr_mod, "BRIEFINGS_DIR", str(tmp_path / "briefings"))
    monkeypatch.setattr(
        dr_mod, "search_topic",
        lambda topic, max_results=4: [
            {"title": f"Result for {topic}", "url": "https://example.com/a", "snippet": "snip"},
        ],
    )
    monkeypatch.setattr(dr_mod, "fetch_page_text", lambda url, max_chars=3000: "page body text")
    monkeypatch.setattr(
        recap_mod, "fetch_feed",
        lambda url: [{"title": "T", "summary": "S", "link": "L", "published": "", "source": "Src"}],
    )

    resp = client.post("/api/recap/run/stream", json={"include_web_search": True})
    assert resp.status_code == 200
    payloads = _parse_sse(resp.text)

    stages = [p["stage"] for p in payloads if "stage" in p]
    # The three pipeline phases must run in order.
    assert "analyze" in stages
    assert "research" in stages
    assert "summarize" in stages
    assert stages.index("analyze") < stages.index("research") < stages.index("summarize")

    # The analyzer surfaces research intents and the researcher surfaces hits.
    # (Empty DB -> the analyzer falls back to the default tech intents.)
    intents = next(p["intents"] for p in payloads if "intents" in p)
    assert isinstance(intents, list) and intents
    searches = [p["search"] for p in payloads if "search" in p]
    assert searches and searches[0]["url"] == "https://example.com/a"

    done = next(p for p in payloads if p.get("done"))
    assert done["summary"].startswith("# Morning Briefing")

    # Briefing persisted to disk and exposed via the today endpoint.
    briefing = client.get("/api/recap/briefing/today").json()
    assert briefing["available"] is True
    assert briefing["markdown"].startswith("# Morning Briefing")


def test_deep_research_no_material_errors(client, monkeypatch, fake_ollama, tmp_path):
    """When web search and RSS both yield nothing, the pipeline errors cleanly."""
    from carrot import recap as recap_mod
    from carrot import deep_research as dr_mod

    monkeypatch.setattr(dr_mod, "OllamaClient", fake_ollama)
    monkeypatch.setattr(dr_mod, "BRIEFINGS_DIR", str(tmp_path / "briefings"))
    monkeypatch.setattr(dr_mod, "search_topic", lambda topic, max_results=4: [])
    monkeypatch.setattr(recap_mod, "fetch_feed", lambda url: [])

    resp = client.post("/api/recap/run/stream", json={"include_web_search": True})
    payloads = _parse_sse(resp.text)
    assert any("error" in p for p in payloads)
    assert not any(p.get("done") for p in payloads)


def test_briefing_today_absent(client, monkeypatch, tmp_path):
    from carrot import deep_research as dr_mod
    monkeypatch.setattr(dr_mod, "BRIEFINGS_DIR", str(tmp_path / "empty"))
    resp = client.get("/api/recap/briefing/today")
    assert resp.json()["available"] is False


# ===== analyzer =====

def test_derive_intents_uses_model_when_signal(fake_ollama):
    from carrot import deep_research as dr_mod
    client = fake_ollama()
    activity = {"messages": ["working on a chess engine"], "goals": [], "reminders": []}
    intents = dr_mod.derive_intents(client, "m", activity)
    # structured_chat on the fake returns these two intents.
    assert intents == ["test intent one", "test intent two"]


def test_derive_intents_falls_back_without_signal(fake_ollama):
    from carrot import deep_research as dr_mod
    client = fake_ollama()
    intents = dr_mod.derive_intents(client, "m", {"messages": [], "goals": [], "reminders": []})
    assert intents == dr_mod.DEFAULT_INTENTS


# ===== scheduler =====

def test_is_due_logic():
    from carrot.deep_research import is_due
    today = datetime(2026, 7, 30, 5, 0)
    # Past the target time and not yet run today -> due.
    assert is_due(today, "04:00", "") is True
    assert is_due(today, "04:00", "2026-07-29") is True
    # Already ran today -> not due.
    assert is_due(today, "04:00", "2026-07-30") is False
    # Before the target time -> not due.
    assert is_due(datetime(2026, 7, 30, 3, 0), "04:00", "") is False


def test_check_overnight_run_disabled(isolated_db):
    from carrot import deep_research as dr_mod
    from carrot.config import set_config
    set_config("recap_auto_enabled", False)
    # Should short-circuit without invoking the pipeline.
    assert dr_mod.check_overnight_run(datetime(2026, 7, 30, 5, 0)) is False


def test_check_overnight_run_fires_once(isolated_db, monkeypatch):
    from carrot import deep_research as dr_mod
    from carrot.config import set_config, get_config

    calls = {"n": 0}
    monkeypatch.setattr(dr_mod, "run_deep_research", lambda **kw: calls.__setitem__("n", calls["n"] + 1))
    set_config("recap_auto_enabled", True)
    set_config("recap_auto_time", "04:00")

    now = datetime(2026, 7, 30, 5, 0)
    assert dr_mod.check_overnight_run(now) is True
    assert calls["n"] == 1
    assert get_config()["recap_auto_last_run"] == "2026-07-30"
    # A second check the same day must not run again.
    assert dr_mod.check_overnight_run(now) is False
    assert calls["n"] == 1
