"""Paying once for the part of the prompt that never changes.

Every round of an agentic turn re-sends the same prefix: the directive and the
whole tool schema, which `prompt_overhead()` measures at roughly a third of an
8k window on its own. That was billed and re-processed on every call — and
since a turn now runs until the context fills rather than for eight rounds,
the same bytes were going over the wire dozens of times per question.

Marking that prefix lets the provider read it back at a fraction of the input
price with a shorter time to first token. The care is in what is *not* marked:
the conversation grows every round, so a marker on it writes a new cache entry
every time and pays the write premium for a prefix nothing will reuse.
"""
import json

import pytest

from carrot import router


def _tools(n=12, size=400):
    return [{"name": f"tool_{i}", "description": "x" * size, "input_schema": {}}
            for i in range(n)]


class TestWhatGetsMarked:
    def test_the_tools_carry_one_breakpoint_on_the_last_one(self):
        """The cache is a prefix: marking the final definition caches every
        definition before it. Four markers is the per-request limit and
        spending them on individual tools caches the same bytes four times."""
        marked = router._cached_tools(_tools())
        assert sum(1 for t in marked if "cache_control" in t) == 1
        assert "cache_control" in marked[-1]

    def test_the_system_prompt_becomes_a_marked_block(self):
        block = router._cached_system("the directive")
        assert block == [{"type": "text", "text": "the directive",
                          "cache_control": {"type": "ephemeral"}}]

    def test_the_originals_are_not_mutated(self):
        """The same tool list is reused across rounds and providers; marking
        it in place would leak an Anthropic-only field into an Ollama call."""
        tools = _tools(3)
        router._cached_tools(tools)
        assert all("cache_control" not in t for t in tools)


class TestWhenItIsWorthIt:
    def test_a_real_tool_schema_is_worth_caching(self):
        assert router._worth_caching("y" * 3000, _tools()) is True

    def test_a_two_line_prompt_is_not(self):
        """A cache write costs more than an ordinary call. Below the floor
        there is nothing to amortise and marking it makes the turn more
        expensive, not less."""
        assert router._worth_caching("hi", []) is False

    def test_the_floor_is_where_the_provider_puts_it(self):
        assert router.CACHE_MIN_TOKENS >= 1024

    def test_a_broken_estimate_declines_rather_than_guessing(self, monkeypatch):
        """Erring towards not caching: the failure mode of a wrong yes is a
        silently pricier turn, and of a wrong no is the status quo."""
        from carrot import context_windows

        monkeypatch.setattr(context_windows, "estimate_tokens",
                            lambda text: (_ for _ in ()).throw(RuntimeError("boom")))
        assert router._worth_caching("y" * 9000, _tools()) is False


class TestTheRequestItBuilds:
    def _request(self, monkeypatch, tools, system="the directive " * 400):
        """Capture the request dict the Anthropic path would send."""
        seen = {}

        class Streamed:
            """Enough of the SDK's stream to get past the request build.

            It yields nothing and reports no tool calls, which is all this
            needs: the assertion is about what went out, not what came back.
            """

            def __iter__(self):
                return iter([])

            def get_final_message(self):
                return type("Message", (), {"content": [], "stop_reason": "end_turn"})()

        class FakeStream:
            def __enter__(self):
                return Streamed()

            def __exit__(self, *a):
                return False

        class FakeMessages:
            def stream(self, **request):
                seen.update(request)
                return FakeStream()

        class FakeClient:
            beta = type("B", (), {"messages": FakeMessages()})()

        monkeypatch.setattr(router, "_client", lambda provider: FakeClient())
        monkeypatch.setattr(router, "_kind_of", lambda resolved: "anthropic")

        class Route:
            provider, model, local, effort = "anthropic", "claude-opus-5", False, "high"

        list(router._stream_once(Route(), [{"role": "system", "content": system},
                                           {"role": "user", "content": "hello"}], tools))
        return seen

    def test_a_turn_with_tools_marks_both(self, monkeypatch):
        request = self._request(monkeypatch, _tools())
        assert isinstance(request["system"], list)
        assert request["system"][0]["cache_control"]["type"] == "ephemeral"
        assert "cache_control" in request["tools"][-1]

    def test_the_conversation_is_never_marked(self, monkeypatch):
        """It grows every round. A marker there writes a fresh entry each
        time and pays the premium for a prefix nothing reuses."""
        request = self._request(monkeypatch, _tools())
        assert not json.dumps(request["messages"]).count("cache_control")

    def test_a_small_turn_is_left_alone(self, monkeypatch):
        request = self._request(monkeypatch, [], system="be helpful")
        assert isinstance(request["system"], str)


class TestTheWindowProbeIsNotRepaid:
    """`OllamaClient` caches a model's ceiling per instance, and a fresh
    instance was being built for every turn — so the cache was always empty
    and every local turn paid an HTTP round trip to /api/show before it could
    send its first token."""

    def test_the_probe_happens_once_per_model(self, monkeypatch):
        from carrot import app as A, ollama_client

        calls = []

        class FakeClient:
            def context_limit(self, model):
                calls.append(model)
                return 32768

        monkeypatch.setattr(ollama_client, "OllamaClient", lambda *a, **k: FakeClient())
        A._PROBED_WINDOWS.clear()

        class Local:
            provider, model, local = "ollama", "gemma4:e4b", True

        for _ in range(5):
            assert A._window_tokens(Local()) > 0
        assert calls == ["gemma4:e4b"]

    def test_a_hosted_route_never_probes_at_all(self, monkeypatch):
        from carrot import app as A, ollama_client

        def explode(*a, **k):
            raise AssertionError("a hosted route asked Ollama about its window")

        monkeypatch.setattr(ollama_client, "OllamaClient", explode)

        class Hosted:
            provider, model, local = "anthropic", "claude-opus-5", False

        assert A._window_tokens(Hosted()) > 0
