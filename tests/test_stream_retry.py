"""A turn the provider refused to start used to be a turn that just ended.

`with_rate_limit_retry` covered the non-streaming calls only, which left the
two paths people actually use — chat and the Code tab — with no retry at all.
A 429 or a dropped connection ended the turn, and on a coding turn that had
already read four files and run a command it ended it with the work done and
nothing written.

The rule that makes this safe is the one research already had to learn: only
before the first event. Once tokens are out they are on the user's screen, and
running the request again replays the answer from the top and appends it to
the half already shown.
"""
import pytest

from carrot import router as router_mod


class Route:
    provider, model, local = "anthropic", "claude-opus-5", False


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """The backoff is real seconds. Patched on the `time` module itself,
    because stream_events imports it inside the function."""
    import time as real_time

    slept = []
    monkeypatch.setattr(real_time, "sleep", lambda seconds: slept.append(seconds))
    return slept


class Throttled(Exception):
    """What a provider client raises when it is rate limiting us."""
    status_code = 429


def test_a_refusal_before_the_first_token_is_retried(monkeypatch):
    attempts = []

    def flaky(resolved, messages, tools=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise Throttled("429 rate limit exceeded")
        yield {"type": "content", "text": "the answer"}

    monkeypatch.setattr(router_mod, "_stream_once", flaky)
    out = list(router_mod.stream_events(Route(), [{"role": "user", "content": "hi"}]))
    assert [e["text"] for e in out] == ["the answer"]
    assert len(attempts) == 3


def test_a_failure_after_tokens_are_out_is_not_retried(monkeypatch):
    """Retrying would replay the answer from the top and glue it onto the half
    already on screen."""
    attempts = []

    def dies_midway(resolved, messages, tools=None):
        attempts.append(1)
        yield {"type": "content", "text": "half an ans"}
        raise Throttled("429 rate limit exceeded")

    monkeypatch.setattr(router_mod, "_stream_once", dies_midway)
    seen = []
    with pytest.raises(Throttled):
        for event in router_mod.stream_events(Route(), [{"role": "user", "content": "hi"}]):
            seen.append(event)
    assert len(attempts) == 1
    assert seen == [{"type": "content", "text": "half an ans"}]


def test_a_failure_that_is_not_worth_retrying_is_raised_at_once(monkeypatch):
    """A bad request is bad every time, and sixty seconds of backoff before
    saying so is worse than saying so."""
    attempts = []

    def refuses(resolved, messages, tools=None):
        attempts.append(1)
        raise ValueError("model 'nope' does not exist")
        yield  # pragma: no cover

    monkeypatch.setattr(router_mod, "_stream_once", refuses)
    with pytest.raises(ValueError):
        list(router_mod.stream_events(Route(), [{"role": "user", "content": "hi"}]))
    assert len(attempts) == 1


def test_it_gives_up_rather_than_retrying_forever(monkeypatch):
    attempts = []

    def always_throttled(resolved, messages, tools=None):
        attempts.append(1)
        raise Throttled("429 rate limit exceeded")
        yield  # pragma: no cover

    monkeypatch.setattr(router_mod, "_stream_once", always_throttled)
    with pytest.raises(Throttled):
        list(router_mod.stream_events(Route(), [{"role": "user", "content": "hi"}]))
    assert len(attempts) == router_mod.RATE_LIMIT_RETRIES + 1


def test_the_caller_can_be_told_it_is_waiting(monkeypatch):
    """A long run that sits silent for thirty seconds is indistinguishable
    from one that has hung."""
    attempts = []

    def flaky(resolved, messages, tools=None):
        attempts.append(1)
        if len(attempts) < 2:
            raise Throttled("429 rate limit exceeded")
        yield {"type": "content", "text": "ok"}

    monkeypatch.setattr(router_mod, "_stream_once", flaky)
    waits = []
    list(router_mod.stream_events(Route(), [{"role": "user", "content": "hi"}],
                                  on_wait=lambda attempt, delay, reason: waits.append(reason)))
    assert waits == ["rate limited"]


def test_the_pacer_hears_about_it_too(monkeypatch):
    """Four subagents streaming at once will each discover the limit by
    walking into it otherwise."""
    told = []

    class FakePacer:
        def on_rate_limited(self, delay):
            told.append(delay)

    from carrot import pacing

    monkeypatch.setattr(pacing, "for_provider", lambda provider: FakePacer())

    attempts = []

    def flaky(resolved, messages, tools=None):
        attempts.append(1)
        if len(attempts) < 2:
            raise Throttled("429 rate limit exceeded")
        yield {"type": "content", "text": "ok"}

    monkeypatch.setattr(router_mod, "_stream_once", flaky)
    list(router_mod.stream_events(Route(), [{"role": "user", "content": "hi"}]))
    assert told, "the pacer was never told about the rate limit"


def test_the_code_tab_shows_a_provider_failure_at_all():
    """It did not. Chat has rendered this event since it existed and the Code
    tab ignored it, so a timeout ended the turn with 'Done', no error, and no
    hint that the reason the file was not written was the model never
    answering."""
    from pathlib import Path

    features = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
                / "features.js").read_text(encoding="utf-8")
    assert "payload.provider_error" in features
    # And the footer must not claim the turn finished.
    assert "'Stopped' : 'Done'" in features
