import random
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from rate_limiter import RateLimiter


class FakeClock:
    """A clock we control by hand, so tests never have to sleep."""

    def __init__(self, start=0.0):
        self._now = start

    def __call__(self):
        return self._now

    def advance(self, seconds):
        self._now += seconds


class SpyClock:
    """A clock that counts how many times it was read."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 0.0


def test_allows_up_to_limit():
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=10, clock=clock)

    assert limiter.allow("pod-a") is True
    assert limiter.allow("pod-a") is True
    assert limiter.allow("pod-a") is True


def test_denies_when_limit_is_reached():
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=10, clock=clock)

    for _ in range(3):
        assert limiter.allow("pod-a") is True

    assert limiter.allow("pod-a") is False


def test_stays_denied_inside_window():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=10, clock=clock)

    assert limiter.allow("pod-a") is True
    clock.advance(5)
    assert limiter.allow("pod-a") is True

    clock.advance(4)  # 9 seconds after the first request
    assert limiter.allow("pod-a") is False


def test_exact_boundary_frees_a_slot():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=10, clock=clock)

    assert limiter.allow("pod-a") is True  # t=0
    clock.advance(1)
    assert limiter.allow("pod-a") is True  # t=1
    assert limiter.allow("pod-a") is False

    clock.advance(9)  # t=10: the request at t=0 is exactly 10 seconds old
    assert limiter.allow("pod-a") is True


def test_only_expired_requests_are_forgotten():
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=10, clock=clock)

    assert limiter.allow("pod-a") is True  # t=0
    clock.advance(2)
    assert limiter.allow("pod-a") is True  # t=2
    clock.advance(2)
    assert limiter.allow("pod-a") is True  # t=4
    assert limiter.allow("pod-a") is False

    clock.advance(6)  # t=10: only the request at t=0 expires
    assert limiter.allow("pod-a") is True
    assert limiter.allow("pod-a") is False


def test_denied_requests_are_not_recorded():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=10, clock=clock)

    assert limiter.allow("pod-a") is True
    assert limiter.allow("pod-a") is True
    for _ in range(5):
        assert limiter.allow("pod-a") is False

    clock.advance(10)
    assert limiter.allow("pod-a") is True
    assert limiter.allow("pod-a") is True
    assert limiter.allow("pod-a") is False


def test_keys_are_independent():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=10, clock=clock)

    assert limiter.allow("pod-a") is True
    assert limiter.allow("pod-a") is True
    assert limiter.allow("pod-a") is False

    assert limiter.allow("pod-b") is True
    assert limiter.allow("pod-b") is True
    assert limiter.allow("pod-b") is False


def test_many_keys_each_get_their_own_limit():
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=10, clock=clock)

    for key in ("pod-a", "pod-b", "pod-c"):
        assert limiter.allow(key) is True
        assert limiter.allow(key) is True
        assert limiter.allow(key) is True
        assert limiter.allow(key) is False


def test_idle_key_can_be_used_again():
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=10, clock=clock)

    assert limiter.allow("pod-a") is True
    assert limiter.allow("pod-a") is False

    clock.advance(100)
    assert limiter.allow("pod-a") is True


def test_repeated_fill_and_expire_cycles():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=10, clock=clock)

    for _ in range(10):
        assert limiter.allow("pod-a") is True
        assert limiter.allow("pod-a") is True
        assert limiter.allow("pod-a") is False
        clock.advance(10)


def test_unhashable_key_raises_type_error():
    limiter = RateLimiter(limit=1, window_seconds=10, clock=FakeClock())

    with pytest.raises(TypeError):
        limiter.allow(["not", "hashable"])


@pytest.mark.parametrize("limit", [0, -1])
def test_limit_must_be_positive(limit):
    with pytest.raises(ValueError):
        RateLimiter(limit=limit, window_seconds=60)


@pytest.mark.parametrize("limit", [True, False, 1.5, "3", None])
def test_limit_must_be_an_integer(limit):
    with pytest.raises(TypeError):
        RateLimiter(limit=limit, window_seconds=60)


@pytest.mark.parametrize("window", [0, -1])
def test_window_must_be_positive(window):
    with pytest.raises(ValueError):
        RateLimiter(limit=1, window_seconds=window)


@pytest.mark.parametrize("window", [float("nan"), float("inf"), float("-inf")])
def test_window_must_be_finite(window):
    with pytest.raises(ValueError):
        RateLimiter(limit=1, window_seconds=window)


@pytest.mark.parametrize("window", [True, "60", None])
def test_window_must_be_a_number(window):
    with pytest.raises(TypeError):
        RateLimiter(limit=1, window_seconds=window)


def test_clock_is_read_once_per_call():
    spy = SpyClock()
    limiter = RateLimiter(limit=1, window_seconds=10, clock=spy)

    assert limiter.allow("pod-a") is True
    assert limiter.allow("pod-a") is False
    assert spy.calls == 2


def test_concurrent_calls_on_one_key_never_exceed_limit():
    limiter = RateLimiter(limit=25, window_seconds=60, clock=FakeClock())
    calls = 100
    barrier = threading.Barrier(calls)

    def call():
        barrier.wait()
        return limiter.allow("pod-a")

    with ThreadPoolExecutor(max_workers=calls) as executor:
        results = list(executor.map(lambda _: call(), range(calls)))

    assert sum(results) == 25


def test_concurrent_calls_on_many_keys_are_independent():
    limiter = RateLimiter(limit=10, window_seconds=60, clock=FakeClock())
    keys = ["pod-a", "pod-b", "pod-c", "pod-d", "pod-e"]
    calls_per_key = 20
    total_calls = len(keys) * calls_per_key
    barrier = threading.Barrier(total_calls)

    def call(key):
        barrier.wait()
        return key, limiter.allow(key)

    with ThreadPoolExecutor(max_workers=total_calls) as executor:
        results = list(executor.map(call, keys * calls_per_key))

    for key in keys:
        allowed = sum(1 for result_key, ok in results if result_key == key and ok)
        assert allowed == 10


def test_concurrent_first_use_of_a_new_key_creates_one_state():
    limiter = RateLimiter(limit=7, window_seconds=60, clock=FakeClock())
    calls = 60
    barrier = threading.Barrier(calls)

    def call():
        barrier.wait()
        return limiter.allow("brand-new-key")

    with ThreadPoolExecutor(max_workers=calls) as executor:
        results = list(executor.map(lambda _: call(), range(calls)))

    assert sum(results) == 7


def test_random_operations_never_exceed_limit():
    rng = random.Random(2024)
    clock = FakeClock()
    limiter = RateLimiter(limit=5, window_seconds=10, clock=clock)
    allowed = {"pod-a": [], "pod-b": []}

    for _ in range(500):
        clock.advance(rng.choice([0, 0, 0.5, 1, 2, 5, 11]))
        key = rng.choice(list(allowed))
        if limiter.allow(key):
            allowed[key].append(clock())

    for times in allowed.values():
        for end in times:
            in_window = sum(1 for t in times if end - 10 < t <= end)
            assert in_window <= 5
