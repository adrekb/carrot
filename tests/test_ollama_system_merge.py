"""Every way into /api/chat merges its system blocks.

`merge_system_messages` exists because a GGUF's chat template is under no
obligation to accept two system messages — Qwen3's raises on the second, Ollama
renders a template exception as HTTP 500, and the turn dies before its first
token. What the user sees is "500 Server Error ... /api/chat", an empty plan,
and an answer written from nothing saying the notes contain no facts.

It was applied to two of the three call sites. `structured_chat` — which is the
one the planner and the search-shaping steps go through — still sent the
transcript raw, so a model that chatted fine failed on anything that plans
first. This holds all of them, because the next call site added is the next one
to miss it.
"""
import inspect
import re

import pytest

from carrot import ollama_client
from carrot.ollama_client import merge_system_messages


def system(*contents):
    return [{"role": "system", "content": c} for c in contents]


class TestTheMergeItself:

    def test_two_system_blocks_become_one(self):
        out = merge_system_messages(system("first", "second")
                                    + [{"role": "user", "content": "hi"}])
        assert [m["role"] for m in out] == ["system", "user"]
        assert out[0]["content"] == "first\n\nsecond"

    def test_one_block_is_left_exactly_alone(self):
        messages = system("only") + [{"role": "user", "content": "hi"}]
        assert merge_system_messages(messages) is messages

    def test_a_later_block_is_hoisted_to_the_front(self):
        """The template offers no faithful place for one in the middle, so
        being present at the top beats being a crash where it stood."""
        out = merge_system_messages([
            {"role": "system", "content": "a"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "b"},
        ])
        assert out[0] == {"role": "system", "content": "a\n\nb"}
        assert out[1]["role"] == "user"

    def test_blank_blocks_do_not_become_blank_lines(self):
        out = merge_system_messages(system("a", "   ", "b")
                                    + [{"role": "user", "content": "hi"}])
        assert out[0]["content"] == "a\n\nb"


class TestEveryCallSiteUsesIt:
    """Read off the source, because the failure this prevents is a 500 from a
    real Ollama and there is no way to provoke it from a unit test."""

    def test_no_chat_call_sends_messages_unmerged(self):
        source = inspect.getsource(ollama_client)
        # Every place that builds a /api/chat body names its messages key.
        assignments = re.findall(r'"messages":\s*([^,\n]+)', source)
        assert assignments, "no message assignments found — has the shape changed?"
        for value in assignments:
            assert "merge_system_messages" in value, (
                f'a chat body sends `{value.strip()}` rather than merging its '
                "system blocks first, which is HTTP 500 on a template that "
                "accepts only one"
            )

    def test_there_are_still_three_of_them(self):
        """If a fourth appears, the assertion above is what should catch it —
        this is here so that adding one is a deliberate act."""
        source = inspect.getsource(ollama_client)
        assert source.count('"messages": merge_system_messages(') == 3
