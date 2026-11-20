import pytest

import rate_limit
from rate_limit import SlidingWindowRateLimiter


class FakeClock:
    """A clock the test moves by hand, so no test ever sleeps."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


def make(clock, max_events=3, window=60):
    return SlidingWindowRateLimiter(max_events, window, clock=clock)


def test_allows_up_to_the_limit_then_blocks(clock):
    limiter = make(clock)

    assert [limiter.allow("u")[0] for _ in range(3)] == [True, True, True]

    allowed, wait = limiter.allow("u")
    assert allowed is False
    assert wait == pytest.approx(60)


def test_retry_after_counts_down_to_when_the_oldest_event_expires(clock):
    limiter = make(clock)

    limiter.allow("u")        # t = 1000, the oldest event
    clock.advance(10)
    limiter.allow("u")
    limiter.allow("u")

    clock.advance(20)         # t = 1030, oldest expires at 1060

    allowed, wait = limiter.allow("u")
    assert allowed is False
    assert wait == pytest.approx(30)


def test_allowed_again_once_the_window_has_passed(clock):
    limiter = make(clock)
    for _ in range(3):
        limiter.allow("u")

    clock.advance(60)  # the oldest events are exactly out of the window

    assert limiter.allow("u") == (True, 0.0)


def test_window_slides_instead_of_resetting(clock):
    """No double burst at a minute boundary: only expired events free a slot."""
    limiter = make(clock)

    limiter.allow("u")        # t = 0
    clock.advance(30)
    limiter.allow("u")        # t = 30
    limiter.allow("u")        # t = 30

    clock.advance(31)         # t = 61: only the t = 0 event has left

    assert limiter.allow("u")[0] is True    # takes the one free slot
    assert limiter.allow("u")[0] is False   # the t = 30 events still count


def test_blocked_attempts_are_not_counted(clock):
    """Hammering the limit must not make the wait longer."""
    limiter = make(clock)
    for _ in range(3):
        limiter.allow("u")    # t = 0: the limit is used up

    clock.advance(30)
    for _ in range(50):
        assert limiter.allow("u")[0] is False   # blocked at t = 30

    clock.advance(30)         # t = 60: the three real events have expired
    # If the 50 blocked attempts had been counted they would still be in
    # the window (until t = 90) and this would be blocked.
    assert limiter.allow("u")[0] is True


def test_each_key_has_its_own_budget(clock):
    limiter = make(clock, max_events=1)

    assert limiter.allow("alice")[0] is True
    assert limiter.allow("alice")[0] is False
    assert limiter.allow("bob")[0] is True


@pytest.mark.parametrize("max_events, window", [(0, 60), (-1, 60), (1, 0), (1, -5)])
def test_rejects_invalid_settings(max_events, window):
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(max_events, window)


def test_quiet_users_are_swept_from_memory(clock, monkeypatch):
    monkeypatch.setattr(rate_limit, "SWEEP_EVERY", 5)
    limiter = make(clock, max_events=10, window=60)

    limiter.allow("quiet")
    clock.advance(120)        # "quiet" is now outside the window

    for _ in range(5):        # the 5th call triggers a sweep
        limiter.allow("active")

    assert "quiet" not in limiter._events
    assert "active" in limiter._events