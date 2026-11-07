"""A small, in-memory, thread-safe rate limiter."""

import math
import threading
import time
from collections import deque


class _KeyState:
    """Everything the limiter remembers for one key."""

    def __init__(self):
        self.lock = threading.Lock()
        self.timestamps = deque()


class RateLimiter:
    """Allow at most ``limit`` requests per key in any ``window_seconds``.

    Each key is counted separately. A key is just a label; (just any name)
    """

    def __init__(self, limit, window_seconds, clock=time.monotonic):
        
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if limit < 1:
            raise ValueError("limit must be at least 1")

        if isinstance(window_seconds, bool) or not isinstance(
            window_seconds, (int, float)
        ):
            raise TypeError("window_seconds must be a number")
        if not math.isfinite(window_seconds):
            raise ValueError("window_seconds must be finite")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than 0")

        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._states = {}
        self._states_lock = threading.Lock()

    def allow(self, key):
        """Return True if a request for ``key`` is allowed right now."""
        state = self._state_for(key)

        # Only one thread may change this key's timestamps at a time.
        with state.lock:
            now = self._clock()
            cutoff = now - self.window_seconds

            # Requests that are exactly one window old, or older, no longer
            # count.
            while state.timestamps and state.timestamps[0] <= cutoff:
                state.timestamps.popleft()

            if len(state.timestamps) < self.limit:
                state.timestamps.append(now)
                return True

            return False

    def _state_for(self, key):
        """Get the state for ``key``, creating it the first time we see it."""
        with self._states_lock:
            state = self._states.get(key)
            if state is None:
                state = _KeyState()
                self._states[key] = state
            return state
