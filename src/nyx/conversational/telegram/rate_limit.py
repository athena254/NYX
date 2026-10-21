"""A bounded per-user sliding-window rate limiter.

Ported as a pattern from The-Agency's beta guard, not copied: in-process, dependency-free,
keyed only by the admitted Telegram user id, with an injectable clock so tests are
deterministic.

Scope, stated plainly because it is easy to over-trust:

* This is **not** authorisation. It sits *behind* the allowlist in :mod:`admission` and only
  paces how fast an already-admitted user can reach the model.
* State is in-process and does not survive a restart. A restart resets every user's budget.
* State growth is bounded: expired entries are evicted before every call, and at most
  ``max_users`` users are tracked. New identities are refused while the table is saturated,
  which is the safe direction.
* The only input is the numeric user id. Invalid identities (``0``, negatives, bools,
  non-ints) are refused without consuming budget or creating state, so a malformed update
  cannot be used to grow the table.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable

_DEFAULT_MAX_USERS = 1000


class RateLimiter:
    """A sliding-window request budget keyed by Telegram user id."""

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        max_users: int = _DEFAULT_MAX_USERS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if type(max_requests) is not int:
            raise TypeError(f"max_requests must be an int, got {type(max_requests).__name__}")
        if max_requests <= 0:
            raise ValueError(f"max_requests must be positive, got {max_requests}")
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, (int, float)):
            raise TypeError(f"window_seconds must be a number, got {type(window_seconds).__name__}")
        window = float(window_seconds)
        if not math.isfinite(window) or window <= 0:
            raise ValueError(f"window_seconds must be a finite positive duration, got {window!r}")
        if type(max_users) is not int:
            raise TypeError(f"max_users must be an int, got {type(max_users).__name__}")
        if max_users <= 0:
            raise ValueError(f"max_users must be positive, got {max_users}")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be a callable returning monotonic seconds")

        self._max_requests = max_requests
        self._window_seconds = window
        self._max_users = max_users
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._hits: dict[int, deque[float]] = {}
        self._last_seen: float | None = None

    @property
    def max_requests(self) -> int:
        return self._max_requests

    @property
    def window_seconds(self) -> float:
        return self._window_seconds

    @property
    def max_users(self) -> int:
        return self._max_users

    def __len__(self) -> int:
        return len(self._hits)

    def _evict_expired(self, now: float) -> None:
        """Drop timestamps outside the window, and users with nothing live left."""

        cutoff = now - self._window_seconds
        for user_id, stamps in list(self._hits.items()):
            live = deque((t for t in stamps if cutoff < t <= now), maxlen=self._max_requests)
            if live:
                self._hits[user_id] = live
            else:
                del self._hits[user_id]

    def allow(self, user_id: int) -> bool:
        """Consume one request of ``user_id``'s budget if any remains.

        Returns ``False`` for invalid identities and when the per-user cap is exhausted,
        without consuming a token in either case.
        """

        if type(user_id) is not int or user_id <= 0:
            return False
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            return False
        moment = float(now)
        if not math.isfinite(moment):
            return False
        # Clamp to the latest seen instant so a backwards clock step can never grant extra
        # budget. The limiter's view of time only moves forward.
        if self._last_seen is not None and moment < self._last_seen:
            moment = self._last_seen
        else:
            self._last_seen = moment

        self._evict_expired(moment)
        stamps = self._hits.get(user_id)
        if stamps is not None:
            if len(stamps) >= self._max_requests:
                return False
            stamps.append(moment)
            return True
        if len(self._hits) >= self._max_users:
            return False
        self._hits[user_id] = deque([moment], maxlen=self._max_requests)
        return True
