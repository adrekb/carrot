"""Adaptive pacing for hosted providers.

Rate limits used to be handled by doing less work: research capped itself to
three workers on a hosted route. That trades away sources — a thinner report,
in a way the reader cannot see — to avoid a 429, and three was a guess.
Pacing meters the request *rate* instead, so a tight limit makes a run slower
rather than shallower.
"""
import time

import pytest

from carrot import pacing


@pytest.fixture(autouse=True)
def fresh_pacers():
    pacing.reset()
    yield
    pacing.reset()


class TestPacer:
    def test_starts_unthrottled(self):
        """An account with room should not be slowed on our say-so."""
        pacer = pacing.Pacer()
        assert pacer.interval == 0
        assert pacer.wait(sleep=lambda s: None) == 0

    def test_a_rate_limit_slows_the_next_request(self):
        pacer = pacing.Pacer()
        assert pacer.on_rate_limited() > 0

    def test_repeated_rate_limits_back_off_further(self):
        pacer = pacing.Pacer()
        first = pacer.on_rate_limited()
        second = pacer.on_rate_limited()
        assert second > first

    def test_backoff_is_bounded(self):
        """Without a ceiling a run that hits a bad patch would stop making
        progress rather than slow down."""
        pacer = pacing.Pacer()
        for _ in range(40):
            pacer.on_rate_limited()
        assert pacer.interval <= pacing.MAX_INTERVAL

    def test_retry_after_is_obeyed_exactly(self):
        """The provider knows its own limit better than any heuristic here."""
        pacer = pacing.Pacer()
        pacer.on_rate_limited(retry_after=7.0)
        assert pacer.interval == 7.0

    def test_retry_after_is_still_capped(self):
        pacer = pacing.Pacer()
        pacer.on_rate_limited(retry_after=10_000)
        assert pacer.interval <= pacing.MAX_INTERVAL

    def test_success_eventually_wins_the_speed_back(self):
        """One bad minute must not slow the rest of a long run forever."""
        pacer = pacing.Pacer()
        throttled = pacer.on_rate_limited()
        for _ in range(pacing.RECOVERY_AFTER_SUCCESSES):
            pacer.on_success()
        assert pacer.interval < throttled

    def test_recovery_reaches_zero(self):
        pacer = pacing.Pacer()
        pacer.on_rate_limited()
        for _ in range(pacing.RECOVERY_AFTER_SUCCESSES * 40):
            pacer.on_success()
        assert pacer.interval == 0

    def test_recovery_is_slower_than_backoff(self):
        """Over-correcting downward just earns another 429."""
        assert pacing.RECOVERY_FACTOR > 1 / pacing.BACKOFF_FACTOR

    def test_success_on_an_unthrottled_pacer_is_a_no_op(self):
        pacer = pacing.Pacer()
        pacer.on_success()
        assert pacer.interval == 0

    def test_waiting_actually_sleeps_once_throttled(self):
        pacer = pacing.Pacer()
        pacer.on_rate_limited(retry_after=2.0)
        slept = []
        pacer.wait(sleep=slept.append)
        assert slept and slept[0] > 0

    def test_concurrent_callers_queue_rather_than_collide(self):
        """Each caller must reserve its own slot. If they all read the same
        `next_at` they would wait the same delay and then fire together —
        which is the burst the pacer exists to prevent."""
        pacer = pacing.Pacer()
        pacer.on_rate_limited(retry_after=1.0)
        delays = [pacer.wait(sleep=lambda s: None) for _ in range(4)]
        assert delays == sorted(delays)
        assert delays[-1] > delays[0], "callers were handed the same slot"


class TestProviderPacers:
    def test_one_pacer_per_provider(self):
        """Two runs against the same account share a limit, so they have to
        share a pacer or each will find the limit by tripping it."""
        assert pacing.for_provider("anthropic") is pacing.for_provider("anthropic")

    def test_providers_are_paced_independently(self):
        assert pacing.for_provider("anthropic") is not pacing.for_provider("openai")

    def test_provider_names_are_case_insensitive(self):
        assert pacing.for_provider("OpenAI") is pacing.for_provider("openai")

    def test_status_reports_which_providers_are_throttled(self):
        pacing.for_provider("anthropic").on_rate_limited()
        status = pacing.status()
        assert status["anthropic"]["throttled"] is True
        assert status["anthropic"]["interval"] > 0


class TestResearchKeepsItsBreadth:
    def test_hosted_routes_are_no_longer_capped_to_fewer_workers(self):
        """The whole point: depth decides how much work happens, pacing decides
        how fast it leaves. A hosted route must not quietly read fewer
        sources."""
        import inspect

        from carrot import research

        source = inspect.getsource(research)
        assert "workers = min(workers, CLOUD_MAX_WORKERS)" not in source, \
            "research is still trading away sources to dodge a rate limit"

    def test_every_depth_keeps_the_worker_count_it_declares(self):
        from carrot import research

        for name, profile in research.DEPTHS.items():
            assert profile["workers"] >= 1


class TestRetryIntegration:
    def test_a_rate_limited_call_slows_the_provider_for_everyone(self):
        """Not just this retry — the other workers must not walk into the same
        wall one after another."""
        from carrot import router

        class Limited(Exception):
            status_code = 429

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Limited("rate limit exceeded")
            return "ok"

        result = router.with_rate_limit_retry(flaky, retries=2, provider="testprov")
        assert result == "ok"
        assert pacing.for_provider("testprov").interval > 0

    def test_a_clean_call_leaves_the_provider_unthrottled(self):
        from carrot import router

        assert router.with_rate_limit_retry(lambda: "fine", provider="testprov") == "fine"
        assert pacing.for_provider("testprov").interval == 0

    def test_no_provider_means_no_pacing(self):
        """Local models are not metered; pacing them would just be slower."""
        from carrot import router

        assert router.with_rate_limit_retry(lambda: "fine") == "fine"
        assert pacing.status() == {}
