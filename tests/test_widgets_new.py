"""Machine load, model speed, headlines and markets.

The static hardware profile has been in the Hub since the beginning — what CPU,
how much RAM, which GPU. What was missing is what any of it is *doing*, which
is the only version of the question that explains why a local turn is slow.
"""
import pytest

from carrot import markets, sysmon, widgets


class TestTheCatalogue:
    @pytest.mark.parametrize("kind", ["system", "throughput", "news", "markets"])
    def test_each_new_widget_is_offered(self, isolated_db, kind):
        assert kind in {w["type"] for w in widgets.list_catalog()}

    @pytest.mark.parametrize("kind", ["system", "throughput", "news", "markets"])
    def test_each_has_a_default_config(self, kind):
        assert kind in widgets.DEFAULT_CONFIG

    def test_markets_defaults_to_no_symbols_rather_than_a_copy(self):
        """The widget falls back to markets.DEFAULT_SYMBOLS while this is
        empty, so the defaults can change without every already-installed
        widget being pinned to the old set."""
        assert widgets.DEFAULT_CONFIG["markets"]["symbols"] == []


class TestThroughput:
    """Read from Ollama's own counters. An external stopwatch includes the
    queue, the prompt evaluation and the socket, and reports a figure well
    below what the model is producing — the sort of plausible wrong number
    that sends someone off to buy a graphics card."""

    def setup_method(self):
        sysmon.throughput.clear()

    def test_a_generation_becomes_a_rate(self):
        sysmon.throughput.record("m", eval_count=300, eval_duration_ns=6_000_000_000)
        assert sysmon.throughput.snapshot()["latest"]["tps"] == 50.0

    def test_a_one_token_reply_is_not_a_measurement(self):
        """The time in a two-token reply is all startup."""
        sysmon.throughput.record("m", eval_count=2, eval_duration_ns=1_000_000_000)
        assert sysmon.throughput.snapshot()["latest"] is None

    def test_a_zero_duration_cannot_divide(self):
        sysmon.throughput.record("m", eval_count=300, eval_duration_ns=0)
        assert sysmon.throughput.snapshot()["latest"] is None

    def test_prompt_reading_is_a_separate_rate(self):
        """A long context is slow to ingest even when generation is fast, and
        one number hides which of the two you are waiting on."""
        sysmon.throughput.record("m", 300, 6_000_000_000,
                                 prompt_eval_count=4000, prompt_eval_duration_ns=2_000_000_000)
        assert sysmon.throughput.snapshot()["latest"]["prompt_tps"] == 2000.0

    def test_it_averages_per_model(self):
        sysmon.throughput.record("a", 100, 1_000_000_000)
        sysmon.throughput.record("b", 200, 1_000_000_000)
        by_model = sysmon.throughput.snapshot()["by_model"]
        assert by_model["a"]["average"] == 100.0
        assert by_model["b"]["average"] == 200.0

    def test_the_window_is_bounded(self):
        for _ in range(sysmon.HISTORY * 3):
            sysmon.throughput.record("m", 100, 1_000_000_000)
        assert len(sysmon.throughput.snapshot()["samples"]) <= sysmon.HISTORY

    def test_a_frame_with_no_metrics_is_not_an_error(self):
        """Every non-final streaming frame is one."""
        sysmon.record_ollama_metrics("m", {"response": "hi"})
        sysmon.record_ollama_metrics("m", None)
        sysmon.record_ollama_metrics("m", {"eval_count": "banana", "eval_duration": None})
        assert sysmon.throughput.snapshot()["latest"] is None


class TestMeters:
    def test_a_reading_has_cpu_and_memory(self):
        m = sysmon.meters()
        assert m["available"] is True
        assert 0 <= m["cpu_percent"] <= 100
        assert 0 <= m["ram_percent"] <= 100
        assert m["ram_total_gb"] > 0

    def test_gpus_are_a_list_even_with_no_card(self):
        assert isinstance(sysmon.meters().get("gpus"), list)

    def test_a_missing_probe_is_empty_not_an_exception(self):
        """A dashboard widget must never be the thing that breaks the
        dashboard."""
        assert sysmon._run(["definitely-not-a-real-command-xyz"]) == ""


class TestMarkets:
    def test_a_ticker_is_sanitised_before_it_reaches_a_url(self):
        # The symbol goes into the URL *path*, so the property that matters is
        # that nothing path-like survives — asserted as a property rather than
        # against a hand-counted expected string.
        dirty = markets._clean("../../etc/passwd?x=1#f")
        assert "/" not in dirty and "?" not in dirty and "#" not in dirty
        assert markets._clean("nvda") == "NVDA"
        assert markets._clean("^GSPC") == "^GSPC"
        assert markets._clean("BTC-USD") == "BTC-USD"
        assert markets._clean("EURUSD=X") == "EURUSD=X"

    def test_the_symbol_count_is_capped(self, monkeypatch):
        monkeypatch.setattr(markets, "_quote", lambda s: None)
        out = markets.quotes([f"SYM{i}" for i in range(50)])
        assert len(out["quotes"]) <= markets.MAX_SYMBOLS

    def test_an_unpriceable_symbol_is_named_not_dropped(self, monkeypatch):
        """A typo in a custom symbol should be visible, not just missing."""
        markets._cache.clear()
        monkeypatch.setattr(markets, "_quote", lambda s: None)
        out = markets.quotes(["NOPE"])
        assert out["quotes"][0]["unavailable"] is True
        assert "NOPE" in out["error"]

    def test_a_failed_poll_serves_the_last_good_reading_marked_stale(self, monkeypatch):
        """A widget that empties itself on one failed poll flickers on any
        connection that is less than perfect."""
        markets._cache.clear()
        good = {"symbol": "X", "label": "X", "price": 10.0, "change": 0.0,
                "change_percent": 0.0, "at": 1}
        monkeypatch.setattr(markets, "_quote", lambda s: good)
        markets.quotes(["X"])
        monkeypatch.setattr(markets, "CACHE_SECONDS", 0)
        monkeypatch.setattr(markets, "_quote", lambda s: None)
        out = markets.quotes(["X"])
        assert out["quotes"][0]["price"] == 10.0
        assert out["quotes"][0]["stale"] is True

    def test_every_quote_carries_when_it_is_from(self):
        """An undated price is indistinguishable from a live one, which is
        the failure that matters — the widget looks identical while being
        hours out of date."""
        markets._cache.clear()
        out = markets.quotes(["^GSPC"])
        quote = out["quotes"][0]
        if not quote.get("unavailable"):
            assert "at" in quote

    def test_the_catalogue_labels_every_default(self):
        labels = {e["symbol"] for e in markets.CATALOGUE}
        assert set(markets.DEFAULT_SYMBOLS) <= labels


class TestTheApi:
    def test_meters_endpoint(self, client):
        assert client.get("/api/system/meters").status_code == 200

    def test_throughput_endpoint(self, client):
        body = client.get("/api/system/throughput").json()
        assert "samples" in body and "by_model" in body

    def test_markets_endpoint_accepts_symbols(self, client):
        body = client.get("/api/markets?symbols=^GSPC").json()
        assert "quotes" in body

    def test_markets_catalogue_endpoint(self, client):
        body = client.get("/api/markets/catalogue").json()
        assert body["catalogue"] and body["default"]

    def test_headlines_endpoint_never_500s(self, client, monkeypatch):
        """A dead feed is a dead widget, not a dead dashboard."""
        from carrot import recap
        def boom():
            raise RuntimeError("no network")
        monkeypatch.setattr(recap, "fetch_all_feeds", boom)
        body = client.get("/api/news/headlines")
        assert body.status_code == 200
        assert body.json()["items"] == []
