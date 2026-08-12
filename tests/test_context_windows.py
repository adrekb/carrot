"""Context windows for models that will not report one.

Ollama answers this question; no hosted provider does. So a Claude or a GPT in
the picker showed no window at all, and a model served by someone's own
endpoint had no way to be told one — which is backwards, because the window is
the most consequential fact about a model for how Carrot behaves, and it was
shown only where it was easiest to obtain.
"""
import pytest

from carrot import context_windows as ctx


class TestTheTable:
    @pytest.mark.parametrize("model,expected", [
        ("claude-opus-4-5-20251101", 200_000),
        ("claude-3-5-sonnet", 200_000),
        ("gpt-4o", 128_000),
        ("gpt-4o-mini-2024-07-18", 128_000),
        ("gemini-2.5-pro", 1_048_576),
        ("codestral-latest", 256_000),
        ("mistral-large-latest", 131_072),
    ])
    def test_known_families_resolve(self, model, expected):
        assert ctx.from_table(model) == expected

    def test_a_version_suffix_does_not_break_the_match(self):
        """Pinning exact ids guarantees the table is stale the week it ships.
        What does not change under a version suffix is the family."""
        assert ctx.from_table("claude-sonnet-4-5-20260114") == \
            ctx.from_table("claude-sonnet-4-5")

    def test_a_provider_qualified_name_matches_on_the_model_part(self):
        assert ctx.from_table("anthropic/claude-opus-4-5") == 200_000
        assert ctx.from_table("openai:gpt-4o") == 128_000

    def test_embedding_models_do_not_inherit_their_familys_window(self):
        """`codestral-embed` carries a chat family's name and is not a chat
        model. A 256k badge on an 8k embedder is a wrong answer, not a
        rounding error."""
        assert ctx.from_table("codestral-embed") == 8_192
        assert ctx.from_table("text-embedding-3-large") == 8_192

    def test_an_unrecognised_model_returns_nothing_rather_than_a_guess(self):
        """A wrong context window is worse than none: it is the number the UI
        would use to tell someone their conversation fits."""
        assert ctx.from_table("my-own-finetune-v3") == 0


class TestPrecedence:
    """probed beats the table; a person beats both."""

    def test_the_table_answers_when_nothing_else_can(self, isolated_db):
        assert ctx.window_for("anthropic", "claude-opus-4-5")["source"] == ctx.SOURCE_KNOWN

    def test_a_probe_beats_the_table(self, isolated_db):
        got = ctx.window_for("ollama", "gemma4:e4b", probed=131_072)
        assert got["source"] == ctx.SOURCE_PROBED
        assert got["tokens"] == 131_072

    def test_an_override_beats_a_probe(self, isolated_db):
        """A person reading a model card beats a regular expression, and beats
        a probe too — they can see what we cannot."""
        ctx.set_override("ollama", "gemma4:e4b", 65_536)
        got = ctx.window_for("ollama", "gemma4:e4b", probed=131_072)
        assert got["source"] == ctx.SOURCE_SET
        assert got["tokens"] == 65_536

    def test_unknown_is_reported_as_unknown(self, isolated_db):
        got = ctx.window_for("custom", "nobody-has-heard-of-this")
        assert got["source"] == ctx.SOURCE_UNKNOWN
        assert got["tokens"] == 0
        assert got["why"]

    def test_the_same_model_from_two_providers_is_told_apart(self, isolated_db):
        ctx.set_override("provider-a", "shared-name", 32_768)
        assert ctx.window_for("provider-a", "shared-name")["tokens"] == 32_768
        assert ctx.window_for("provider-b", "shared-name")["source"] == ctx.SOURCE_UNKNOWN

    def test_an_override_can_be_cleared(self, isolated_db):
        ctx.set_override("x", "m", 32_768)
        ctx.set_override("x", "m", None)
        assert ctx.window_for("x", "m")["source"] == ctx.SOURCE_UNKNOWN


class TestOverridesAreValidated:
    """Validated in the module, not at the edge, because there is more than
    one edge: a settings form, the model picker and a direct config write all
    land here, and a 0 from any of them is a window the router divides by."""

    @pytest.mark.parametrize("bad", [0, -1, 12, 99_000_000])
    def test_nonsense_is_refused(self, isolated_db, bad):
        with pytest.raises(ValueError):
            ctx.set_override("x", "m", bad)

    def test_a_stored_value_out_of_range_is_ignored_on_read(self, isolated_db):
        """Config can be edited by hand, so reading has to be as careful as
        writing."""
        from carrot import config
        config.set_config(ctx.OVERRIDE_KEY, {"x/m": 5, "y/n": "banana", "z/o": 32768})
        assert ctx.window_for("x", "m")["source"] == ctx.SOURCE_UNKNOWN
        assert ctx.window_for("y", "n")["source"] == ctx.SOURCE_UNKNOWN
        assert ctx.window_for("z", "o")["tokens"] == 32_768


class TestOverheadIsMeasuredNotWritten:
    """The figure the Advanced box quotes.

    "8,192 tokens" reads as eight thousand tokens of conversation and is
    nothing of the sort — the directive and the tool schemas are in the window
    before the question is. A number hardcoded into the settings copy would be
    wrong by the next release and nobody would find out.
    """

    def test_the_overhead_is_real_and_substantial(self, isolated_db):
        from carrot import app as A
        overhead = A.prompt_overhead()
        assert overhead["worst"] > 1000, "the disclaimer would be quoting nothing"
        for mode in ("off", "single", "multi"):
            assert overhead[mode]["tokens"] > 0
            assert overhead[mode]["tools"] > 0

    def test_more_tools_and_a_longer_directive_cost_more(self, isolated_db):
        from carrot import app as A
        overhead = A.prompt_overhead()
        assert overhead["off"]["tokens"] < overhead["multi"]["tokens"]

    def test_the_quoted_figure_is_the_worst_case(self, isolated_db):
        """Quoting the average would understate it for exactly the user who
        has multi-turn search on and the smallest window set."""
        from carrot import app as A
        overhead = A.prompt_overhead()
        assert overhead["worst"] == max(
            overhead[m]["tokens"] for m in ("off", "single", "multi"))


class TestTheApi:
    def test_the_models_endpoint_carries_a_window_per_model(self, client):
        body = client.get("/api/models").json()
        assert "windows" in body and "overhead" in body

    def test_a_window_can_be_set_over_http(self, client):
        body = client.put("/api/models/context-window", json={
            "provider": "custom", "model": "my-endpoint", "tokens": 65536,
        })
        assert body.status_code == 200
        assert body.json()["window"]["tokens"] == 65536
        assert body.json()["window"]["source"] == ctx.SOURCE_SET

    def test_a_nonsense_window_is_a_400_not_a_stored_zero(self, client):
        assert client.put("/api/models/context-window", json={
            "provider": "custom", "model": "m", "tokens": 3,
        }).status_code == 400
