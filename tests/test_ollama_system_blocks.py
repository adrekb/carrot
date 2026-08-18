"""A local turn dies before its first token if the prompt has two system blocks.

Carrot writes the system half of a prompt as a stack: answer style, the search
directive, a skill's instructions, the workspace's rules, the calendar, the
ambient roster, what it remembers, the rolling summary. Eight independent
switches, eight separate messages, and the OpenAI-shaped API they are modelled
on takes as many as you like.

Qwen3's GGUF template does not:

    {%- if message.role == "system" %}
        {%- if not loop.first %}
            {{- raise_exception('System message must be at the beginning.') }}

`loop.first` is true at index 0 only, so the *second* block is already an error.
Ollama renders a template exception as HTTP 500, which arrives as "provider
stopped the turn: 500 Server Error ... /api/chat" — a sentence that points at
the model, the server and the network, and not at the shape of the prompt.

Measured against `hf.co/empero-ai/Qwen3.8-9B-GGUF:Q6_K`: one system block
answers, two return 500, and the turn ends with an empty plan and no searches.
"""
import pytest

from carrot.ollama_client import merge_system_messages


def roles(messages):
    return [m["role"] for m in messages]


class TestTheStackBecomesOneBlock:
    def test_several_system_blocks_are_merged(self):
        merged = merge_system_messages([
            {"role": "system", "content": "Answer plainly."},
            {"role": "system", "content": "Search when unsure."},
            {"role": "user", "content": "hi"},
        ])
        assert roles(merged) == ["system", "user"]

    def test_the_text_of_every_block_survives(self):
        """Merging must not be a way of dropping the seventh instruction."""
        merged = merge_system_messages([
            {"role": "system", "content": "Answer plainly."},
            {"role": "system", "content": "Search when unsure."},
            {"role": "system", "content": "Today is Tuesday."},
            {"role": "user", "content": "hi"},
        ])
        assert "Answer plainly." in merged[0]["content"]
        assert "Search when unsure." in merged[0]["content"]
        assert "Today is Tuesday." in merged[0]["content"]

    def test_the_blocks_keep_their_order(self):
        """Order is load-bearing: style is written first so that a skill's
        instructions later in the prompt can still override it."""
        merged = merge_system_messages([
            {"role": "system", "content": "first"},
            {"role": "system", "content": "second"},
            {"role": "user", "content": "hi"},
        ])
        content = merged[0]["content"]
        assert content.index("first") < content.index("second")

    def test_the_blocks_are_separated(self):
        """Joined end to end, the last line of one directive runs into the
        first line of the next and both stop reading as instructions."""
        merged = merge_system_messages([
            {"role": "system", "content": "first"},
            {"role": "system", "content": "second"},
            {"role": "user", "content": "hi"},
        ])
        assert "first\n\nsecond" in merged[0]["content"]


class TestTheTranscriptIsLeftAlone:
    def test_the_conversation_keeps_its_order(self):
        merged = merge_system_messages([
            {"role": "system", "content": "a"},
            {"role": "system", "content": "b"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ])
        assert roles(merged) == ["system", "user", "assistant", "user"]
        assert [m["content"] for m in merged[1:]] == ["one", "two", "three"]

    def test_other_keys_on_a_turn_survive(self):
        """Ollama takes images on the message itself, so rebuilding a user turn
        instead of passing it through would silently drop the attachment."""
        merged = merge_system_messages([
            {"role": "system", "content": "a"},
            {"role": "system", "content": "b"},
            {"role": "user", "content": "look", "images": ["base64..."]},
        ])
        assert merged[-1]["images"] == ["base64..."]

    def test_a_tool_result_is_not_disturbed(self):
        merged = merge_system_messages([
            {"role": "system", "content": "a"},
            {"role": "system", "content": "b"},
            {"role": "user", "content": "q"},
            {"role": "tool", "content": "result"},
        ])
        assert roles(merged) == ["system", "user", "tool"]


class TestItDoesNothingWhenThereIsNothingToDo:
    @pytest.mark.parametrize("messages", [
        [{"role": "user", "content": "hi"}],
        [{"role": "system", "content": "a"}, {"role": "user", "content": "hi"}],
        [],
    ])
    def test_a_prompt_that_is_already_fine_is_passed_through(self, messages):
        """Identity, not a copy — nothing downstream should have to wonder
        whether it is holding the same list it passed in."""
        assert merge_system_messages(messages) is messages


class TestTheAwkwardShapes:
    def test_a_system_block_after_the_transcript_is_hoisted(self):
        """There are none today — Carrot assembles the whole stack before the
        transcript — but the template offers no faithful place to put one, so
        being present at the top beats being a crash in the middle."""
        merged = merge_system_messages([
            {"role": "system", "content": "a"},
            {"role": "user", "content": "q"},
            {"role": "system", "content": "late"},
        ])
        assert roles(merged) == ["system", "user"]
        assert "late" in merged[0]["content"]

    def test_an_empty_block_does_not_leave_a_blank_gap(self):
        """A directive that switched itself off contributes nothing, and two
        blank lines in the middle of a prompt read as a section break."""
        merged = merge_system_messages([
            {"role": "system", "content": "a"},
            {"role": "system", "content": "   "},
            {"role": "system", "content": "b"},
            {"role": "user", "content": "hi"},
        ])
        assert merged[0]["content"] == "a\n\nb"

    def test_blocks_that_are_all_empty_leave_no_system_message(self):
        """An empty system message is not harmless: it is still a message, and
        the template counts messages rather than characters."""
        merged = merge_system_messages([
            {"role": "system", "content": ""},
            {"role": "system", "content": "  "},
            {"role": "user", "content": "hi"},
        ])
        assert roles(merged) == ["user"]


class TestEveryPathThatPostsMessagesUsesIt:
    """Two of the three send a message list, and the one that was missed is
    the one whose failure nobody would attribute to this."""

    @pytest.mark.parametrize("method", ["chat_stream_events", "chat"])
    def test_the_method_merges_before_posting(self, method):
        import inspect

        from carrot import ollama_client

        source = inspect.getsource(getattr(ollama_client.OllamaClient, method))
        assert "merge_system_messages(messages)" in source, (
            f"{method} posts the caller's message list unchanged"
        )
