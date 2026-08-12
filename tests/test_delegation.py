"""Handing one step to a different model.

Routing has always been per turn: the whole conversation goes wherever the
picker says. A local 4B that hits one genuinely hard sub-problem halfway
through had two options, both bad — grind at it, or make the user notice,
switch model and ask again with the turn's context gone.

This buys one step. Most of what follows is about the ways that could go
wrong: unbounded recursion, the user's routing being bypassed, and a turn
quietly costing four frontier calls while looking like one local turn.
"""
import pytest

from carrot import agent_tools as t, router as router_mod


class FakeRoute:
    provider, model, local = "anthropic", "claude-opus-5", False


class TestTheDelegateIsBoxedIn:
    def test_it_is_given_no_tools(self):
        """A delegate that could call tools could call `ask_model`, and there
        is no natural bottom to that. A recursion limit would be arbitrary
        where "it cannot recurse" is exact."""
        captured = {}

        def fake_complete(route, messages, **kwargs):
            captured["kwargs"] = kwargs
            captured["messages"] = messages
            return "an answer"

        import carrot.router as r
        original = r.complete
        r.complete = fake_complete
        r_route = r.route
        r.route = lambda task=None, **k: FakeRoute()
        try:
            t._tool_ask_model(question="why?", task="reasoning")
        finally:
            r.complete, r.route = original, r_route
        assert "tools" not in captured["kwargs"]

    def test_it_is_given_no_conversation_history(self):
        captured = {}

        import carrot.router as r
        original, original_route = r.complete, r.route
        r.complete = lambda route, messages, **k: captured.setdefault("m", messages) and "x" or "x"
        r.route = lambda task=None, **k: FakeRoute()
        try:
            t._tool_ask_model(question="why?", context="the code is here")
        finally:
            r.complete, r.route = original, original_route
        roles = [m["role"] for m in captured["m"]]
        assert roles == ["system", "user"], roles

    def test_the_delegate_is_told_not_to_guess(self):
        """A confident answer built on information it was not given is worse
        than useless: the receiving model cannot tell and will pass it on."""
        assert "Do not guess" in t.DELEGATE_SYSTEM

    def test_an_overlong_context_is_clipped_rather_than_refused(self):
        captured = {}
        import carrot.router as r
        original, original_route = r.complete, r.route
        r.complete = lambda route, messages, **k: captured.setdefault("m", messages) and "x" or "x"
        r.route = lambda task=None, **k: FakeRoute()
        try:
            out = t._tool_ask_model(question="why?", context="x" * 50_000)
        finally:
            r.complete, r.route = original, original_route
        assert not out.startswith("error:")
        assert len(captured["m"][1]["content"]) <= t.MAX_DELEGATION_CHARS + 40


class TestTheUsersRoutingIsRespected:
    def test_the_target_is_named_by_task_not_by_model(self):
        """Letting the model name a model would route around the user's own
        configuration and, on a metered provider, spend their money on a
        model they did not choose."""
        params = t.TOOLS["ask_model"]["parameters"]["properties"]
        assert "task" in params
        assert "model" not in params
        assert "provider" not in params

    def test_an_unknown_task_is_refused_with_the_real_list(self, isolated_db):
        out = t._tool_ask_model(question="why?", task="not-a-real-task")
        assert out.startswith("error:")
        assert "reasoning" in out

    def test_the_task_resolves_through_the_ordinary_router(self, isolated_db):
        seen = {}
        import carrot.router as r
        original, original_route = r.complete, r.route
        r.route = lambda task=None, **k: seen.setdefault("task", task) and FakeRoute() or FakeRoute()
        r.complete = lambda *a, **k: "answer"
        try:
            t._tool_ask_model(question="why?", task="code")
        finally:
            r.complete, r.route = original, original_route
        assert seen["task"] == "code"


class TestFailureAndAttribution:
    def _run(self, complete):
        import carrot.router as r
        original, original_route = r.complete, r.route
        r.complete, r.route = complete, (lambda task=None, **k: FakeRoute())
        try:
            return t._tool_ask_model(question="why?")
        finally:
            r.complete, r.route = original, original_route

    def test_a_provider_failure_is_a_fact_not_an_exception(self):
        """The delegating model has a turn to finish, and "the specialist was
        unreachable" is something it can work around."""
        def boom(*a, **k):
            raise RuntimeError("429 rate limited")
        out = self._run(boom)
        assert out.startswith("error:")
        assert "Carry on without it" in out

    def test_an_empty_reply_is_reported(self):
        assert self._run(lambda *a, **k: "   ").startswith("error:")

    def test_the_answer_says_who_gave_it(self):
        """The delegating model absorbs a delegate's answer as its own, and
        the user is entitled to know it came from somewhere else."""
        out = self._run(lambda *a, **k: "because of X")
        assert "answered by anthropic/claude-opus-5" in out

    def test_a_missing_question_is_refused(self):
        assert t._tool_ask_model(question="  ").startswith("error:")


class TestItIsWiredIn:
    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath(*parts).read_text(encoding="utf-8")

    def test_the_tool_is_offered_in_every_search_mode(self, isolated_db):
        from carrot import app as A
        for mode in ("off", "single", "multi"):
            names = {x["function"]["name"] for x in A._available_tools(mode)}
            assert "carrot__ask_model" in names, mode

    def test_delegations_are_capped_per_turn(self):
        from carrot import app as A
        assert A.MAX_DELEGATIONS >= 1
        app = self.read("carrot", "app.py")
        assert "delegations >= MAX_DELEGATIONS" in app

    def test_the_cap_is_enforced_not_merely_requested(self):
        """A limit stated in a tool description is a request, and the thing
        being limited costs the user money."""
        app = self.read("carrot", "app.py")
        block = app[app.index('if bare == "ask_model":'):]
        block = block[:block.index("yield {\"tool\": {\"name\": name, \"args\": args}}")]
        assert "rejected" in block and "continue" in block

    def test_the_delegation_survives_a_reload(self):
        app = self.read("carrot", "app.py")
        assert '"delegation"' in app[app.index("TRACE_EVENTS = ("):]
        js = self.read("carrot", "web", "js", "app.js")
        assert "event.delegation" in js, "not replayed"
        assert "payload.delegation" in js, "not streamed"

    def test_it_is_not_treated_as_mutating(self):
        """It changes nothing. An approval prompt on every one would make it
        unusable for the case it exists for."""
        assert t.TOOLS["ask_model"]["mutating"] is False
