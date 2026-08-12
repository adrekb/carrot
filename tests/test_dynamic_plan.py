"""A plan that can be revised while it runs.

The plan was drafted from the question alone and could then only tick. That is
the wrong shape for the thing it models — you find out what a task involves by
starting it — so a step the first file read makes pointless had to be ground
through, and the step that same file made necessary could not be expressed.

The reason it stayed fixed is the whole difficulty: a plan the model can
shorten is a plan the model will shorten. The search gate and the goal nudges
exist *because* models stop early. So adding is cheap and dropping is
expensive, and most of these tests are about dropping.
"""
import pytest

from carrot import app as A


class TestAdding:
    def test_a_new_step_is_accepted(self, isolated_db, monkeypatch):
        monkeypatch.setattr(A.router_mod, "complete", lambda *a, **k:
                            '{"add": ["read config.py to find the flag"], "drop": []}')
        out = A._replan(None, "q", ["step one", "step two"], ["step two"], "found things")
        assert out["add"] == ["read config.py to find the flag"]

    def test_a_step_already_on_the_list_is_not_added_again(self, isolated_db, monkeypatch):
        """Restating the plan back at us would double an entry and un-tick it."""
        monkeypatch.setattr(A.router_mod, "complete", lambda *a, **k:
                            '{"add": ["Step one"], "drop": []}')
        out = A._replan(None, "q", ["step one", "step two"], ["step two"], "e")
        assert out["add"] == []

    def test_the_plan_cannot_grow_without_limit(self, isolated_db, monkeypatch):
        many = ", ".join(f'"a new step number {i}"' for i in range(20))
        monkeypatch.setattr(A.router_mod, "complete", lambda *a, **k:
                            '{"add": [%s], "drop": []}' % many)
        goals = ["one goal here", "two goal here"]
        out = A._replan(None, "q", goals, goals, "e")
        assert len(out["add"]) <= A.MAX_GOALS - len(goals)


class TestDroppingIsExpensive:
    """The loophole: a model that can delete the step it has not done makes
    every gate in this file advisory."""

    GOALS = ["find the engine specification", "find the production date",
             "find the price"]

    def _replan(self, monkeypatch, payload):
        monkeypatch.setattr(A.router_mod, "complete", lambda *a, **k: payload)
        return A._replan(None, "q", list(self.GOALS), list(self.GOALS), "evidence")

    def test_a_fact_about_the_world_is_a_valid_reason(self, isolated_db, monkeypatch):
        out = self._replan(monkeypatch, '{"add": [], "drop": [{"step": "find the price",'
                           ' "reason": "the manufacturer has not announced pricing"}]}')
        assert [d["step"] for d in out["drop"]] == ["find the price"]

    @pytest.mark.parametrize("excuse", [
        "not needed", "this is out of scope", "redundant with the others",
        "already covered by the answer", "not required for this question",
        "we have enough information", "it would take too long",
        "optional given the time budget", "can be omitted",
    ])
    def test_an_excuse_about_the_run_is_refused(self, isolated_db, monkeypatch, excuse):
        """A reason about the run rather than about the world is the model
        excusing itself, and is exactly how the gate gets bypassed."""
        out = self._replan(monkeypatch,
                           '{"add": [], "drop": [{"step": "find the price", "reason": "%s"}]}'
                           % excuse)
        assert out["drop"] == [], excuse

    def test_a_drop_with_no_reason_at_all_is_refused(self, isolated_db, monkeypatch):
        out = self._replan(monkeypatch,
                           '{"add": [], "drop": [{"step": "find the price", "reason": ""}]}')
        assert out["drop"] == []

    def test_a_paraphrased_step_deletes_nothing(self, isolated_db, monkeypatch):
        """Matched against the real list rather than trusting the quote — a
        paraphrase would delete nothing and report that it had."""
        out = self._replan(monkeypatch, '{"add": [], "drop": [{"step": "the price one",'
                           ' "reason": "the manufacturer has not announced pricing"}]}')
        assert out["drop"] == []

    def test_the_plan_cannot_empty_itself(self, isolated_db, monkeypatch):
        """A run with no steps left has no gate, so "drop everything" is the
        shortest path to finishing."""
        drops = ", ".join(
            '{"step": "%s", "reason": "the vehicle was never produced"}' % g
            for g in self.GOALS)
        out = self._replan(monkeypatch, '{"add": [], "drop": [%s]}' % drops)
        assert len(out["drop"]) < len(self.GOALS)


class TestFailureIsQuiet:
    """A revision step that could fail the turn would make every long run more
    fragile in exchange for a refinement."""

    def test_unparseable_json_leaves_the_plan_alone(self, isolated_db, monkeypatch):
        monkeypatch.setattr(A.router_mod, "complete", lambda *a, **k: "sorry, no.")
        assert A._replan(None, "q", ["a step here"], ["a step here"], "e") == \
            {"add": [], "drop": []}

    def test_a_provider_error_leaves_the_plan_alone(self, isolated_db, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("429")
        monkeypatch.setattr(A.router_mod, "complete", boom)
        assert A._replan(None, "q", ["a step here"], ["a step here"], "e") == \
            {"add": [], "drop": []}

    def test_no_goals_means_no_model_call(self, isolated_db, monkeypatch):
        called = []
        monkeypatch.setattr(A.router_mod, "complete",
                            lambda *a, **k: called.append(1) or "{}")
        assert A._replan(None, "q", [], [], "e") == {"add": [], "drop": []}
        assert not called


class TestItIsWiredIn:
    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath(*parts).read_text(encoding="utf-8")

    def test_revisions_are_capped(self):
        assert A.MAX_REPLANS >= 1
        app = self.read("carrot", "app.py")
        assert "replans < MAX_REPLANS" in app

    def test_the_plan_is_only_revised_while_there_is_budget_to_act_on_it(self):
        """Revising a plan there is no round left to work on changes nothing
        except the picture the user is looking at."""
        app = self.read("carrot", "app.py")
        assert "rounds_left_now > 0" in app

    def test_added_steps_are_told_to_the_model(self):
        # A step added to the picture and not to the conversation is a step
        # the model has no idea it acquired.
        app = self.read("carrot", "app.py")
        assert "The plan has grown from what you found" in app

    def test_the_change_is_shown_not_just_applied(self):
        app = self.read("carrot", "app.py")
        assert '"added": revision["add"]' in app
        assert '"dropped": revision["drop"]' in app
        js = self.read("carrot", "web", "js", "app.js")
        assert "plan.dropped" in js and "plan-why" in js

    def test_a_dropped_step_keeps_its_row(self):
        """Removing it outright would make the run look tidier than it was."""
        css = self.read("carrot", "web", "css", "style.css")
        assert ".plan-item.dropped" in css
        assert ".plan-why" in css
