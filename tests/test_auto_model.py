"""Auto: letting the message pick the task, and the task pick the model.

Routing was already per-task and already configurable — but a chat turn took
`req.model` straight from the picker, so nothing ever looked at what was
actually asked. You always chose, every time, including for the messages where
you had no opinion.

Auto is deliberately thin: it names a task and hands the decision to
`route()`. Everything below is about it staying thin — it must not become a
second routing layer, must not outrank a model the user picked, and must not
change the model silently.
"""
from pathlib import Path

import pytest

from carrot import app as A, router as router_mod


def read_js(name="app.js"):
    root = Path(__file__).resolve().parents[1]
    return (root / "carrot" / "web" / "js" / name).read_text(encoding="utf-8")


class TestClassifyMessage:
    """Patterns, not a model call — so the patterns are the contract."""

    @pytest.mark.parametrize("message", [
        "why is this failing?\n```python\nprint(x)\n```",
        "Traceback (most recent call last):\n  File \"a.py\", line 2",
        "diff --git a/app.py b/app.py",
        "have a look at carrot/router.py and tell me what it does",
        "getting a syntax error on the second line",
        "git rebase is telling me there is a merge conflict",
    ])
    def test_code_evidence_wins(self, message):
        assert router_mod.classify_message(message)["task"] == router_mod.TASK_CODE

    @pytest.mark.parametrize("message", [
        "walk me through this step by step",
        "prove that the sum of two odd numbers is even",
        "what are the trade-offs between renting and buying?",
        "why does inflation lag interest rate changes?",
        "analyse the second quarter numbers for me",
    ])
    def test_reasoning_evidence_wins(self, message):
        assert router_mod.classify_message(message)["task"] == router_mod.TASK_REASONING

    @pytest.mark.parametrize("message", [
        "hey, how's it going?",
        "remind me what we talked about yesterday",
        "give me three names for a cat",
    ])
    def test_everything_else_is_chat(self, message):
        assert router_mod.classify_message(message)["task"] == router_mod.TASK_CHAT

    def test_a_shown_traceback_beats_a_reasoning_word(self):
        # "why does" is a reasoning signal, but a pasted traceback is a code
        # question first — the evidence outranks the phrasing.
        verdict = router_mod.classify_message(
            "why does this happen?\nTraceback (most recent call last):"
        )
        assert verdict["task"] == router_mod.TASK_CODE

    def test_code_stated_as_a_question_reads_as_reasoning(self):
        # No code in sight, and the ask is for a judgement. Both tasks escalate
        # the same way by default, so this is about the reason being truthful.
        verdict = router_mod.classify_message("how would I approach for restructuring this?")
        assert verdict["task"] == router_mod.TASK_REASONING

    def test_a_long_question_gets_the_stronger_model(self):
        message = "Here is the situation. " * 40 + " What should I do?"
        assert router_mod.classify_message(message)["task"] == router_mod.TASK_REASONING

    def test_a_long_statement_is_not_a_hard_question(self):
        assert router_mod.classify_message("la la la " * 200)["task"] == router_mod.TASK_CHAT

    def test_the_code_tab_is_believed_over_the_text(self):
        verdict = router_mod.classify_message("hello", coder=True)
        assert verdict["task"] == router_mod.TASK_CODE

    def test_an_empty_message_is_chat_not_a_crash(self):
        assert router_mod.classify_message("")["task"] == router_mod.TASK_CHAT
        assert router_mod.classify_message(None)["task"] == router_mod.TASK_CHAT

    def test_every_verdict_carries_a_reason(self):
        for message in ["```x```", "prove it", "hello", ""]:
            assert router_mod.classify_message(message)["reason"]

    def test_it_never_names_a_background_task(self):
        # classify/extract/summarize are Carrot's own work. Routing a user's
        # message to one would send it to a task nobody opted into.
        tasks = {task for task, _, _ in router_mod._AUTO_RULES}
        assert not (tasks & router_mod.LOCAL_ONLY_TASKS)


class TestAutoRoute:
    """It must resolve *through* `route()`, not around it."""

    def test_it_routes_the_task_it_named(self, isolated_db):
        router_mod.set_route("reasoning", "gemma4:e4b", provider="ollama")
        resolved = router_mod.auto_route("prove that this terminates")
        assert resolved.task == router_mod.TASK_REASONING
        assert resolved.auto is True

    def test_an_assignment_still_wins(self, isolated_db):
        router_mod.set_route("code", "codellama:7b", provider="ollama")
        resolved = router_mod.auto_route("fix the bug in carrot/app.py")
        assert resolved.model == "codellama:7b"

    def test_the_reason_says_what_it_read_and_where_it_went(self, isolated_db):
        resolved = router_mod.auto_route("```python\nx=1\n```")
        assert resolved.reason.startswith("auto:")
        assert "code" in resolved.reason

    def test_a_plain_route_is_never_marked_auto(self, isolated_db):
        assert router_mod.route("chat").auto is False
        assert router_mod.route("chat").as_dict()["auto"] is False


class TestAutoSetting:
    def test_it_is_off_until_asked_for(self, isolated_db):
        assert router_mod.auto_enabled() is False

    def test_it_survives_a_round_trip(self, isolated_db):
        router_mod.set_auto(True)
        assert router_mod.auto_enabled() is True
        router_mod.set_auto(False)
        assert router_mod.auto_enabled() is False

    def test_turning_it_off_leaves_assignments_alone(self, isolated_db):
        # Assignments are what Auto routes *through*. Clearing them would mean
        # turning Auto off silently threw away the Settings table.
        router_mod.set_route("code", "codellama:7b", provider="ollama")
        router_mod.set_auto(True)
        router_mod.set_auto(False)
        assert router_mod.assignment("code")["model"] == "codellama:7b"

    def test_auto_is_local_while_nothing_escalates(self, isolated_db):
        assert router_mod.auto_is_local() is True

    def test_one_escalating_task_is_enough_to_stop_the_promise(self, isolated_db):
        # The empty state says "everything runs on your machine". Under Auto
        # that is false the moment *any* reachable task can leave — the answer
        # you happen to get next is not what the claim is about.
        from unittest.mock import patch

        with patch.object(router_mod.providers_mod, "usable", lambda p: True):
            router_mod.set_route("code", "claude-opus-5", provider="anthropic")
            assert router_mod.auto_is_local() is False
            assert router_mod.route("chat").local is True


class TestChatHonoursAuto:
    """`_resolve_chat_route` is the only place the two paths meet."""

    class Req:
        message = ""
        model = None
        provider = None
        task = None
        cloud = False
        coder = False
        auto = None

    def req(self, **kwargs):
        r = self.Req()
        for key, value in kwargs.items():
            setattr(r, key, value)
        return r

    def test_the_flag_turns_it_on_for_one_turn(self, isolated_db, fake_ollama):
        resolved = A._resolve_chat_route(
            self.req(message="prove that this terminates", auto=True))
        assert resolved.auto is True
        assert resolved.task == router_mod.TASK_REASONING

    def test_omitting_the_flag_follows_the_setting(self, isolated_db, fake_ollama):
        router_mod.set_auto(True)
        assert A._resolve_chat_route(self.req(message="prove it")).auto is True

    def test_the_flag_can_opt_one_turn_out(self, isolated_db, fake_ollama):
        router_mod.set_auto(True)
        assert A._resolve_chat_route(self.req(message="prove it", auto=False)).auto is False

    def test_a_picked_model_outranks_the_classifier(self, isolated_db, fake_ollama):
        # This is the whole precedence rule: an explicit model is what the user
        # chose, and Auto supplies a missing answer rather than replacing one.
        router_mod.set_auto(True)
        resolved = A._resolve_chat_route(
            self.req(message="```python\nx=1\n```", model="gemma4:e4b"))
        assert resolved.auto is False
        assert resolved.model == "gemma4:e4b"

    def test_a_named_task_outranks_the_classifier(self, isolated_db, fake_ollama):
        router_mod.set_auto(True)
        resolved = A._resolve_chat_route(self.req(message="```x```", task="recap"))
        assert resolved.auto is False
        assert resolved.task == "recap"


class TestAutoApi:
    def test_the_picker_is_told_whether_auto_is_on(self, client):
        body = client.get("/api/models").json()
        assert body["auto"] is False
        assert body["auto_local"] is True

    def test_turning_it_on_sticks(self, client):
        assert client.post("/api/models/auto", json={"enabled": True}).json()["auto"] is True
        assert client.get("/api/models").json()["auto"] is True

    def test_naming_a_model_turns_auto_off(self, client):
        client.post("/api/models/auto", json={"enabled": True})
        client.post("/api/models/select", json={"model": "gemma4:e4b"})
        assert client.get("/api/models").json()["auto"] is False

    def test_status_reports_it_too(self, client):
        assert client.get("/api/router/status").json()["auto"] is False


class TestPickerWiring:
    """The UI half: Auto has to be pickable, and has to stop claiming things."""

    def test_the_popover_has_a_slot_for_auto(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "carrot" / "web" / "index.html").read_text(encoding="utf-8")
        assert 'id="model-auto"' in html

    def test_an_auto_turn_sends_no_model(self):
        source = read_js()
        assert "model: autoModel ? null : currentModel," in source
        assert "auto: autoModel," in source

    def test_the_label_does_not_name_a_model_under_auto(self):
        assert "autoModel ? 'Auto' : currentModel" in read_js()

    def test_the_privacy_line_asks_the_server_what_auto_can_reach(self):
        # "Everything runs on your machine" under Auto is only true if none of
        # the reachable tasks escalates, and only the server knows that.
        #
        # Asserted on the decision rather than on its indentation: it now lives
        # in `answersStayLocal`, shared with the status chip in the rail so the
        # two renderings of this fact cannot disagree.
        js = read_js()
        body = js[js.index("function answersStayLocal"):]
        body = body[:body.index("\n}")]
        assert "autoModel" in body and "autoIsLocal" in body

    def test_the_route_line_explains_an_unchosen_model(self):
        assert "payload.route.auto && payload.route.reason" in read_js()
