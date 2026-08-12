"""The recap, driven by what you have actually been asking about.

It used to be four fixed feeds, a hardcoded DuckDuckGo query for "latest tech
breakthroughs AI programming science news", and a prompt telling the model to
brief "a CS student". The same briefing for everybody — and uncited, which is
the part that matters most: the one thing here that runs unattended, that you
read before you are properly awake and are least likely to check, was the one
thing with none of Research's verification behind it.
"""
import json

import pytest

from carrot import interests, recap


def _add_message(conversation_id, role, content, when="2099-01-01T00:00:00+00:00"):
    from carrot.database import get_db
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at) "
                 "VALUES (?, ?, ?, ?)", (conversation_id, "t", when, when))
    conn.execute("INSERT INTO messages (conversation_id, role, content, timestamp) "
                 "VALUES (?, ?, ?, ?)", (conversation_id, role, content, when))
    conn.commit()
    conn.close()


class TestReadingTheHistory:
    def test_only_the_users_own_messages_are_read(self, isolated_db):
        """Assistant replies are far longer and would swamp any term count
        with Carrot's own vocabulary — a briefing about the words Carrot likes
        to use."""
        _add_message("c1", "user", "what is the F-35 radar cross section")
        _add_message("c1", "assistant", "A very long reply about many things")
        texts = [q["text"] for q in interests.recent_questions()]
        assert texts == ["what is the F-35 radar cross section"]

    def test_previous_recaps_are_not_read_back(self, isolated_db):
        """Otherwise the briefing is self-reinforcing: yesterday's recap
        mentioned drones, so today's decides you are interested in drones,
        forever."""
        _add_message("recap_20260101", "user", "[BBC] Drone news")
        _add_message("c1", "user", "a real question about aircraft")
        ids = {q["conversation_id"] for q in interests.recent_questions()}
        assert "recap_20260101" not in ids

    def test_old_messages_fall_out_of_the_window(self, isolated_db):
        _add_message("c1", "user", "ancient question", when="2020-01-01T00:00:00+00:00")
        assert interests.recent_questions(days=7) == []


class TestRecurrenceNotVolume:
    """One long conversation about a tax form is not an interest, it is an
    errand. This is the single most important rule in the module."""

    def test_one_conversation_is_not_an_interest(self, isolated_db, monkeypatch):
        import carrot.router as r
        called = []
        for i in range(10):
            _add_message("only-one", "user", f"more about my tax form part {i}")
        monkeypatch.setattr(r, "complete", lambda *a, **k: called.append(1) or "{}")
        out = interests.derive_topics()
        assert out["topics"] == []
        assert "single conversation" in out["why"]
        assert not called, "the model was asked about a single-conversation week"

    def test_too_little_history_is_reported_not_guessed_at(self, isolated_db):
        _add_message("c1", "user", "one thing")
        out = interests.derive_topics()
        assert out["topics"] == []
        assert out["why"]

    def test_topics_come_back_with_their_evidence(self, isolated_db, monkeypatch):
        import carrot.router as r
        _add_message("c1", "user", "what is the F-35 radar cross section")
        _add_message("c2", "user", "how does the F-22 compare")
        monkeypatch.setattr(r, "complete", lambda *a, **k: json.dumps({"topics": [
            {"topic": "military aircraft",
             "why": "asked about the F-35 radar cross-section and again about the F-22",
             "questions": ["What is new in fifth-generation fighter development?"]},
        ]}))
        out = interests.derive_topics()
        assert out["topics"][0]["topic"] == "military aircraft"
        # Shown to the user: a subject they cannot trace back to something
        # they said reads as a guess about their personality.
        assert "F-35" in out["topics"][0]["why"]

    def test_a_nonsense_topic_is_dropped(self, isolated_db, monkeypatch):
        import carrot.router as r
        _add_message("c1", "user", "a question about one thing")
        _add_message("c2", "user", "a question about another")
        monkeypatch.setattr(r, "complete", lambda *a, **k: json.dumps({"topics": [
            {"topic": "", "why": "x"},
            {"topic": "x" * 200, "why": "y"},
            "not even a dict",
        ]}))
        assert interests.derive_topics()["topics"] == []

    def test_a_model_failure_does_not_raise(self, isolated_db, monkeypatch):
        import carrot.router as r
        _add_message("c1", "user", "a question")
        _add_message("c2", "user", "another question")

        def boom(*a, **k):
            raise RuntimeError("offline")

        monkeypatch.setattr(r, "complete", boom)
        out = interests.derive_topics()
        assert out["topics"] == []
        assert "offline" in out["why"]


class TestTheQuery:
    def test_the_models_own_question_is_preferred(self):
        assert interests.topic_query({
            "topic": "aircraft",
            "questions": ["What changed in fighter procurement this month?"],
        }).startswith("What changed")

    def test_there_is_always_a_question(self):
        """It was written knowing what the person asked; without one, a plain
        "what is new" still beats researching nothing."""
        assert "aircraft" in interests.topic_query({"topic": "aircraft", "questions": []})

    def test_a_uselessly_short_suggestion_is_ignored(self):
        assert interests.topic_query(
            {"topic": "aircraft", "questions": ["hm"]}).startswith("What is new")


class TestTheBriefing:
    def test_no_interests_falls_back_rather_than_failing(self, isolated_db, monkeypatch):
        """A fresh install genuinely has no interests to read, and inventing
        one would be worse than the fixed feed list it replaced."""
        monkeypatch.setattr(interests, "derive_topics",
                            lambda **k: {"topics": [], "why": "nothing yet"})
        monkeypatch.setattr(recap, "run_recap_stream",
                            lambda *a, **k: iter([{"done": True, "summary": "general"}]))
        events = list(recap.run_interest_recap_stream())
        assert any(e.get("fallback") for e in events)
        assert events[-1]["summary"] == "general"

    def test_the_topics_are_shown_before_the_research_runs(self, isolated_db, monkeypatch):
        """The user should see what Carrot concluded about them *before* it
        spends two minutes acting on it."""
        from carrot import research
        monkeypatch.setattr(interests, "derive_topics", lambda **k: {
            "topics": [{"topic": "aircraft", "why": "you asked twice", "questions": []}],
            "questions": 4, "conversations": 2,
        })
        monkeypatch.setattr(research, "run_research_stream", lambda *a, **k: iter([
            {"done": True, "report": "A cited report.", "sources": 3},
        ]))
        events = list(recap.run_interest_recap_stream())
        topics_at = next(i for i, e in enumerate(events) if "topics" in e)
        research_at = next(i for i, e in enumerate(events)
                           if e.get("stage") == "research")
        assert topics_at < research_at

    def test_the_briefing_is_built_from_research_reports(self, isolated_db, monkeypatch):
        from carrot import research
        monkeypatch.setattr(interests, "derive_topics", lambda **k: {
            "topics": [{"topic": "aircraft", "why": "you asked twice", "questions": []}],
            "questions": 4, "conversations": 2,
        })
        monkeypatch.setattr(research, "run_research_stream", lambda *a, **k: iter([
            {"source": {"id": "S1", "tier": "official"}},
            {"done": True, "report": "The F-35A costs [S1].", "sources": 1},
        ]))
        events = list(recap.run_interest_recap_stream())
        assert any("source" in e for e in events), "the trace lost its sources"
        done = events[-1]
        assert "[S1]" in done["summary"], "the citation did not survive"
        assert "aircraft" in done["summary"]

    def test_a_topic_that_cannot_be_researched_does_not_kill_the_briefing(
            self, isolated_db, monkeypatch):
        from carrot import research
        monkeypatch.setattr(interests, "derive_topics", lambda **k: {
            "topics": [{"topic": "a", "why": "w", "questions": []},
                       {"topic": "b", "why": "w", "questions": []}],
            "questions": 4, "conversations": 2,
        })
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return iter([{"error": "web search returned nothing"}])
            return iter([{"done": True, "report": "Report for b.", "sources": 2}])

        monkeypatch.setattr(research, "run_research_stream", flaky)
        done = list(recap.run_interest_recap_stream())[-1]
        assert done.get("done") and "Report for b." in done["summary"]


class TestTheApi:
    def test_interests_are_readable_on_their_own(self, client):
        """An assistant that has formed a view about what you care about
        should let you look at it without triggering a research run."""
        body = client.get("/api/recap/interests")
        assert body.status_code == 200
        assert "topics" in body.json()
