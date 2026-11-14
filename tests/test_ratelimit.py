"""The token bucket, and the 429 it produces.

The clock is injected everywhere here, so a minute of traffic takes no time and
the tests are not flaky on a loaded machine.
"""

import sys
import threading

import pytest

from paysleuth.ratelimit import DEFAULT_BURST, IDLE_EVICT, MAX_BUCKETS, RateLimiter


THREADS = 24
TAKES = 400          # 9,600 takes: without the lock a lost update is a certainty


class Clock:
    def __init__(self, at=1000.0):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, seconds):
        self.at += seconds


def test_a_caller_may_burst_then_is_refused():
    clock = Clock()
    limiter = RateLimiter(rate=60, burst=5, clock=clock)
    assert [limiter.take("a") for _ in range(5)] == [None] * 5
    assert limiter.take("a") is not None


def test_a_refusal_says_how_long_to_wait_and_is_true():
    clock = Clock()
    limiter = RateLimiter(rate=60, burst=1, clock=clock)   # one a second
    assert limiter.take("a") is None
    wait = limiter.take("a")
    assert wait >= 1

    clock.advance(wait - 0.01)
    assert limiter.take("a") is not None, "allowed before the time it asked for"
    clock.advance(0.02)
    assert limiter.take("a") is None, "still refused after the time it asked for"


def test_buckets_refill_at_the_stated_rate():
    clock = Clock()
    limiter = RateLimiter(rate=120, burst=2, clock=clock)  # two a second
    limiter.take("a"), limiter.take("a")
    assert limiter.take("a") is not None

    clock.advance(0.5)
    assert limiter.take("a") is None
    assert limiter.take("a") is not None


def test_a_bucket_never_refills_past_its_burst():
    clock = Clock()
    limiter = RateLimiter(rate=60, burst=3, clock=clock)
    limiter.take("a")
    clock.advance(3600)                                     # idle for an hour
    assert [limiter.take("a") for _ in range(3)] == [None] * 3
    assert limiter.take("a") is not None, "an idle hour bought more than the burst"


def test_callers_do_not_share_a_bucket():
    limiter = RateLimiter(rate=60, burst=1, clock=Clock())
    assert limiter.take("a") is None
    assert limiter.take("b") is None, "b was refused because a had spent its tokens"
    assert limiter.take("a") is not None


def test_concurrent_takes_hand_out_exactly_the_burst():
    """FastAPI runs plain def endpoints in a thread pool, so this really happens.

    The interpreter switches threads every few milliseconds by default, which is
    long enough that a short unlocked read-modify-write usually completes before
    anything else runs -- so a missing lock passes a naive version of this test.
    Shortening the switch interval and doing far more takes makes the race the
    normal case rather than the lucky one; without the lock this fails every run.
    """
    limiter = RateLimiter(rate=60, burst=TAKES * THREADS // 2, clock=Clock())
    allowed, lock, start = [], threading.Lock(), threading.Barrier(THREADS)
    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)

    def hammer():
        mine = 0
        start.wait()
        for _ in range(TAKES):
            if limiter.take("same") is None:
                mine += 1
        with lock:
            allowed.append(mine)

    try:
        threads = [threading.Thread(target=hammer) for _ in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(original)
    assert sum(allowed) == TAKES * THREADS // 2, "the bucket handed out more tokens than it held"


def test_idle_buckets_are_forgotten():
    clock = Clock()
    limiter = RateLimiter(rate=60, burst=1, clock=clock)
    limiter.take("old")
    clock.advance(IDLE_EVICT + 1)
    limiter.take("new")                                     # eviction runs for new keys
    assert "old" not in limiter._buckets


def test_unknown_keys_cannot_grow_the_table_without_bound():
    limiter = RateLimiter(rate=60, burst=1, clock=Clock())
    for n in range(MAX_BUCKETS + 200):
        limiter.take(f"key-{n}")
    assert len(limiter._buckets) <= MAX_BUCKETS


@pytest.mark.parametrize("rate, burst", [(0, 10), (10, 0), (-1, 10)])
def test_a_nonsense_limit_is_refused_at_construction(rate, burst):
    with pytest.raises(ValueError):
        RateLimiter(rate=rate, burst=burst)


# --- through the API -------------------------------------------------------------------

def test_the_api_refuses_a_flood_with_429_and_retry_after(api_limited):
    api, clock = api_limited
    for _ in range(3):
        assert api.client.get("/members").status_code == 200

    refused = api.client.get("/members")
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1
    assert "retry in" in refused.json()["detail"]

    clock.advance(60)
    assert api.client.get("/members").status_code == 200


def test_health_is_never_rate_limited(api_limited):
    """What the uptime check and the scheduled jobs poll."""
    api, _ = api_limited
    for _ in range(20):
        assert api.client.get("/health").status_code == 200


def test_one_institution_cannot_spend_another_institutions_budget(api_limited):
    api, _ = api_limited
    for _ in range(3):
        api.client.get("/members")
    assert api.client.get("/members").status_code == 429

    other = api.client.get("/members", headers={"X-API-Key": api.other_key})
    assert other.status_code == 200, "a noisy tenant refused a quiet one"


def test_an_unauthenticated_flood_is_limited_too(api_limited):
    api, _ = api_limited
    codes = [api.client.get("/review", headers={"X-API-Key": ""}).status_code for _ in range(6)]
    assert 429 in codes, "an unauthenticated caller had no bucket at all"


@pytest.fixture
def api_limited(api, monkeypatch):
    """The app fixture, re-pointed at a tiny bucket on a clock we control."""
    clock = Clock()
    api.client.app.state.limiter = RateLimiter(rate=DEFAULT_BURST, burst=3, clock=clock)
    return api, clock
