"""Adaptive request pacing for hosted providers.

The first answer to being rate limited was to do less: cap research to three
workers when it routes to a hosted model. That trades away depth to avoid a
429, and it is the wrong trade — the user asked for a thorough report, not a
fast one, and a report built from fewer sources is worse in a way they cannot
see. It is also a guess: three workers is either still too many for a free
tier or needlessly few for a paid one.

Pacing separates the two. How *much* work a run does stays a question of
depth. How *fast* the requests leave is a question of what the provider will
accept, and that can be measured rather than guessed:

* Requests pass through a token bucket, so a burst is smoothed into a rate.
* A 429 multiplies the interval — back off hard, because the provider has
  already told us we are over.
* Sustained success decays the interval back down, so one bad minute does not
  slow the rest of the run forever.
* `Retry-After` is obeyed exactly when the provider sends one; it knows its
  own limit better than any heuristic here.

The result is that a long research run self-tunes to whatever the account
actually allows, and the visible effect of a tight limit is that the run takes
longer — not that it reads fewer sources.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional

# Interval between requests to one provider, in seconds. Starts at zero: an
# account with room should not be slowed on our say-so, only on the
# provider's.
MIN_INTERVAL = 0.0
MAX_INTERVAL = 20.0
# What a 429 does to the interval. A first 429 with no interval set has to
# produce something non-zero, hence the floor.
BACKOFF_FACTOR = 2.0
FIRST_BACKOFF = 1.0
# How fast a quiet period earns the speed back. Deliberately slower than the
# backoff: over-correcting downward just earns another 429.
RECOVERY_FACTOR = 0.85
RECOVERY_AFTER_SUCCESSES = 5


class Pacer:
    """Serialises requests to one provider at an adaptive minimum interval."""

    def __init__(self, name: str = ""):
        self.name = name
        self._lock = threading.Lock()
        self._interval = MIN_INTERVAL
        self._next_at = 0.0
        self._successes = 0
        # Purely for the UI: it is worth being able to say "slowed to one
        # request every 4s because the provider is rate limiting".
        self.throttled_until = 0.0

    @property
    def interval(self) -> float:
        return self._interval

    def wait(self, sleep: Callable[[float], None] = time.sleep) -> float:
        """Block until this request is allowed to go. Returns seconds waited."""
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            # Reserve this slot before releasing the lock, so concurrent
            # callers queue behind each other instead of all waiting the same
            # delay and then firing at once.
            self._next_at = max(now, self._next_at) + self._interval
        if delay > 0:
            sleep(delay)
        return delay

    def on_rate_limited(self, retry_after: Optional[float] = None) -> float:
        """Called after a 429. Returns the new interval."""
        with self._lock:
            self._successes = 0
            if retry_after and retry_after > 0:
                # The provider told us exactly how long; do not argue with it.
                self._interval = min(max(self._interval, float(retry_after)), MAX_INTERVAL)
                self._next_at = time.monotonic() + float(retry_after)
            else:
                base = self._interval or FIRST_BACKOFF
                self._interval = min(base * BACKOFF_FACTOR, MAX_INTERVAL)
                self._next_at = time.monotonic() + self._interval
            self.throttled_until = time.monotonic() + self._interval
            return self._interval

    def on_success(self) -> float:
        """Called after a request the provider accepted."""
        with self._lock:
            if self._interval <= 0:
                return 0.0
            self._successes += 1
            if self._successes >= RECOVERY_AFTER_SUCCESSES:
                self._successes = 0
                self._interval *= RECOVERY_FACTOR
                if self._interval < 0.05:      # close enough to unthrottled
                    self._interval = 0.0
            return self._interval

    def status(self) -> Dict[str, float]:
        with self._lock:
            return {
                "interval": round(self._interval, 2),
                "throttled": self._interval > 0,
            }


_pacers: Dict[str, Pacer] = {}
_pacers_lock = threading.Lock()


def for_provider(provider: str) -> Pacer:
    """The shared pacer for a provider.

    One per provider, not per run: two research runs against the same account
    share the same rate limit, so they have to share the pacer or they will
    each discover the limit separately by tripping it.
    """
    key = (provider or "default").lower()
    with _pacers_lock:
        pacer = _pacers.get(key)
        if pacer is None:
            pacer = Pacer(key)
            _pacers[key] = pacer
        return pacer


def status() -> Dict[str, Dict[str, float]]:
    with _pacers_lock:
        return {name: pacer.status() for name, pacer in _pacers.items()}


def reset():
    """Test hook — pacers are process-global and would leak between tests."""
    with _pacers_lock:
        _pacers.clear()
