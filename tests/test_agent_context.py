"""A run sees the conversation it is in.

Agent mode sits in a conversation, under the same composer as chat, so the
second task is very often about the first: "format that into a nice answer",
"now the other one", "try again without the login". A run used to be built from
the task string alone — `conversation_id` was written onto the run row and
never read back — so "format that" arrived meaning nothing, the plan was
written against the words alone, and the agent looked at an empty browser and
asked the user to paste in the thing it had produced a minute earlier.
"""
import pytest

from carrot import agent, conversation


def conv(*messages):
    return {"messages": [{"role": r, "content": c} for r, c in messages]}


class TestPriorTurns:

    def test_recent_turns_are_rendered_for_the_prompt(self, monkeypatch):
        monkeypatch.setattr(conversation, "get_conversation", lambda cid: conv(
            ("user", "c8 zr1X specs"),
            ("assistant", "0-60 in 2 sec, 1250 hp."),
        ))
        out = agent.prior_turns("abc")
        assert "User: c8 zr1X specs" in out
        assert "You: 0-60 in 2 sec, 1250 hp." in out

    def test_only_the_last_few_turns(self, monkeypatch):
        monkeypatch.setattr(conversation, "get_conversation", lambda cid: conv(
            *[("user", f"turn {n}") for n in range(20)]
        ))
        out = agent.prior_turns("abc")
        assert "turn 19" in out
        assert "turn 0" not in out
        assert out.count("User:") == agent.PRIOR_TURNS

    def test_a_long_answer_is_clipped_rather_than_carried_whole(self, monkeypatch):
        """A previous agent run's answer can be pages long. This is context for
        resolving what the task refers to, not the work itself."""
        monkeypatch.setattr(conversation, "get_conversation", lambda cid: conv(
            ("assistant", "x" * 10_000),
        ))
        out = agent.prior_turns("abc")
        assert len(out) < agent.PRIOR_MESSAGE_CHARS + 100
        assert out.endswith("…")

    def test_no_conversation_is_simply_no_context(self):
        assert agent.prior_turns(None) == ""
        assert agent.prior_turns("") == ""

    def test_a_conversation_that_cannot_be_read_does_not_break_the_run(self, monkeypatch):
        """This runs before the plan, on the path to every agent task. A
        conversation that has gone missing must cost the run its context, not
        the run itself — which is what a NameError here already did once."""
        def boom(cid):
            raise RuntimeError("no such conversation")
        monkeypatch.setattr(conversation, "get_conversation", boom)
        assert agent.prior_turns("gone") == ""

    def test_empty_messages_are_skipped(self, monkeypatch):
        monkeypatch.setattr(conversation, "get_conversation", lambda cid: conv(
            ("user", "   "), ("assistant", "real"),
        ))
        assert agent.prior_turns("abc") == "You: real"


class TestThePromptCarriesIt:

    def test_the_plan_prompt_includes_the_earlier_turns(self, monkeypatch):
        seen = {}

        def capture(model, messages):
            seen["prompt"] = messages[-1]["content"]
            return "1. do it"
        monkeypatch.setattr(agent.router_mod, "complete", capture)
        monkeypatch.setattr(agent.router_mod, "route", lambda **k: "m")

        agent.make_plan("format that", "browser", "User: c8 zr1X specs")
        assert "c8 zr1X specs" in seen["prompt"]
        assert "EARLIER IN THIS CONVERSATION" in seen["prompt"]

    def test_with_nothing_earlier_the_prompt_gains_no_empty_heading(self, monkeypatch):
        seen = {}

        def capture(model, messages):
            seen["prompt"] = messages[-1]["content"]
            return "1. do it"
        monkeypatch.setattr(agent.router_mod, "complete", capture)
        monkeypatch.setattr(agent.router_mod, "route", lambda **k: "m")

        agent.make_plan("do a thing", "browser", "")
        assert "EARLIER IN THIS CONVERSATION" not in seen["prompt"]
