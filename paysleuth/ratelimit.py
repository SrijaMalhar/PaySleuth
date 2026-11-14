"""A token bucket per caller.

Money endpoints are cheap individually and expensive in a loop: a single key
replaying the review queue can make the database the slowest part of someone
else's month. This is the bound.

A bucket holds `burst` tokens and refills at `rate` a minute. A request takes
one. An empty bucket is refused with how long until the next token, so a
well-behaved client can back off exactly rather than guess.

In process, not in PostgreSQL. A database-backed limiter would be exact across
instances but would add a write to every request, including the ones it is
meant to make cheap -- and this deployment is one instance. With N instances
the effective limit is N times the configured one, which is a property to state
rather than a bug to hide; the fix at that point is a shared store, not a
better bucket.

Thread-safe because FastAPI runs plain `def` endpoints in a thread pool, so two
requests for one key really are concurrent.
"""

import threading
import time
from dataclasses import dataclass, field

DEFAULT_RATE = 120          # tokens a minute
DEFAULT_BURST = 30          # how many may arrive at once
IDLE_EVICT = 600.0          # seconds before a silent caller's bucket is forgotten
MAX_BUCKETS = 10_000        # a cap, so unknown keys cannot grow this without bound


@dataclass
class _Bucket:
    tokens: float
    last: float = field(default=0.0)


class RateLimiter:
    """Refuses a caller that is going faster than `rate`, and says for how long."""

    def __init__(self, rate: int = DEFAULT_RATE, burst: int = DEFAULT_BURST,
                 clock=time.monotonic):
        if rate <= 0 or burst <= 0:
            raise ValueError("rate and burst must be positive")
        self.rate, self.burst, self._clock = rate, burst, clock
        self._per_second = rate / 60.0
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> float | None:
        """None if the request may proceed, else seconds until it could.

        The refill is computed from the clock rather than by a timer, so a
        bucket that has not been touched for an hour is full when it is, and no
        work happens for callers who are not calling.
        """
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._evict(now)
                bucket = self._buckets[key] = _Bucket(tokens=float(self.burst), last=now)
            else:
                elapsed = max(0.0, now - bucket.last)
                bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * self._per_second)
            bucket.last = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return None
            # Round up: telling a client 0.4s when a token lands in 0.5s invites
            # a second refusal, and two refusals read as a broken limiter.
            return max(1, int((1.0 - bucket.tokens) / self._per_second + 0.999))

    def _evict(self, now: float) -> None:
        """Drop buckets nobody has used, so unknown keys cannot grow this forever."""
        if len(self._buckets) < MAX_BUCKETS:
            stale = [k for k, b in self._buckets.items() if now - b.last > IDLE_EVICT]
        else:
            # Over the cap, forget the least recently seen half rather than refuse
            # service: a full bucket is the safe thing to lose.
            ordered = sorted(self._buckets.items(), key=lambda kv: kv[1].last)
            stale = [k for k, _ in ordered[: len(ordered) // 2]]
        for key in stale:
            del self._buckets[key]

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)
