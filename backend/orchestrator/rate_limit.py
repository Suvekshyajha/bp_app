import os
import threading
import time
from collections import deque
from collections.abc import Callable

from dotenv import load_dotenv


load_dotenv()


# How often (in calls) to throw away users who have gone quiet
SWEEP_EVERY = 1000


class SlidingWindowRateLimiter:
    """Allow at most `max_events` per `window_seconds` for each key.

    "Sliding" means the window always looks back from right now, so there is
    no moment (like the start of a new minute) where a user can suddenly
    send a double burst.

    Counters live in this process's memory: they reset when the server
    restarts, and each worker process counts separately. For several
    workers or servers, keep the counters in Redis instead.
    """

    def __init__(
        self,
        max_events: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:

        if max_events < 1 or window_seconds <= 0:
            raise ValueError("max_events and window_seconds must be positive")

        self.max_events = max_events
        self.window_seconds = window_seconds
        self._clock = clock

        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def allow(self, key: str) -> tuple[bool, float]:
        """Count one event for `key`.

        Returns (True, 0.0) if it is allowed, or (False, seconds_to_wait)
        if the key is over its limit. Blocked attempts are not counted, so
        hammering the limit does not make the wait longer.
        """

        now = self._clock()

        with self._lock:
            self._calls += 1

            if self._calls % SWEEP_EVERY == 0:
                self._sweep(now)

            events = self._events.setdefault(key, deque())

            # Forget events that have left the window
            cutoff = now - self.window_seconds

            while events and events[0] <= cutoff:
                events.popleft()

            if len(events) >= self.max_events:
                # Free again once the oldest event leaves the window
                wait = events[0] + self.window_seconds - now
                return False, max(wait, 0.0)

            events.append(now)
            return True, 0.0

    def _sweep(self, now: float) -> None:
        cutoff = now - self.window_seconds

        quiet = [
            key
            for key, events in self._events.items()
            if not events or events[-1] <= cutoff
        ]

        for key in quiet:
            del self._events[key]


# Chat messages: this many per user in any window of this many seconds
CHAT_RATE_LIMIT_MESSAGES = int(os.getenv("CHAT_RATE_LIMIT_MESSAGES", "10"))
CHAT_RATE_LIMIT_WINDOW_SECONDS = float(
    os.getenv("CHAT_RATE_LIMIT_WINDOW_SECONDS", "60")
)

chat_limiter = SlidingWindowRateLimiter(
    max_events=CHAT_RATE_LIMIT_MESSAGES,
    window_seconds=CHAT_RATE_LIMIT_WINDOW_SECONDS,
)


# Document uploads: this many per user in any window of this many seconds
DOC_UPLOAD_RATE_LIMIT = int(os.getenv("DOC_UPLOAD_RATE_LIMIT", "5"))
DOC_UPLOAD_RATE_LIMIT_WINDOW_SECONDS = float(
    os.getenv("DOC_UPLOAD_RATE_LIMIT_WINDOW_SECONDS", "60")
)

doc_upload_limiter = SlidingWindowRateLimiter(
    max_events=DOC_UPLOAD_RATE_LIMIT,
    window_seconds=DOC_UPLOAD_RATE_LIMIT_WINDOW_SECONDS,
)
