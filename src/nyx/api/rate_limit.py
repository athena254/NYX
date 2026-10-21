"""Per-caller sliding-window rate limiting for the HTTP API.

The limiter is deliberately in-memory and per-process. It protects one API process from a
single caller consuming unbounded work; it is not a cluster-wide quota. See
``docs/operations.md`` for the documented non-claim.

A sliding window is used instead of a fixed window so a burst straddling a window boundary
cannot receive twice the configured allowance.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from nyx.api.errors import ApiError, api_error_handler, request_id_for
from nyx.auth.dependency import current_principal
from nyx.error_codes import ErrorCode

_logger = logging.getLogger("nyx.api.rate_limit")


@dataclass(frozen=True)
class RateLimitDecision:
    """The outcome of recording one hit against a caller's budget."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: float


class SlidingWindowRateLimiter:
    """A per-key sliding-window limiter with an injectable monotonic clock.

    Only allowed hits are recorded. A refused hit does not extend the window, so the
    advertised ``retry_after_seconds`` is the time at which the next request can succeed.
    """

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._limit = limit
        self._window_seconds = float(window_seconds)
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> RateLimitDecision:
        """Record a hit for ``key`` and return the decision for this request."""

        now = self._clock()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits

            cutoff = now - self._window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self._limit:
                retry_after = hits[0] + self._window_seconds - now
                # A backwards clock step must not produce an unbounded or negative hint.
                bounded = min(self._window_seconds, max(0.0, retry_after))
                return RateLimitDecision(
                    allowed=False,
                    limit=self._limit,
                    remaining=0,
                    retry_after_seconds=bounded,
                )

            hits.append(now)
            return RateLimitDecision(
                allowed=True,
                limit=self._limit,
                remaining=self._limit - len(hits),
                retry_after_seconds=0.0,
            )

    def reset(self) -> None:
        """Forget every caller's history."""

        with self._lock:
            self._hits.clear()


def principal_or_host_key(request: Request) -> str:
    """Key by authenticated principal when the deployment authenticates callers, else client host.

    The limiter runs as middleware, which is *before* FastAPI resolves route dependencies, so the
    principal cannot be read off ``request.state`` -- it is resolved here. An unauthenticated
    caller, including one whose credential is invalid, falls back to the client host, so a
    deployment that authenticates still rate-limits an unknown caller instead of letting it through.

    The host is the socket peer, never a caller-controlled header such as ``X-Forwarded-For`` or
    ``X-API-Key``.
    """

    app = getattr(request, "app", None)
    settings = getattr(getattr(app, "state", None), "settings", None)
    if settings is not None and settings.auth_mode not in ("none", "local"):
        try:
            principal = current_principal(request)
        except ApiError:
            principal = None
        if principal is not None:
            # AuthenticatedPrincipal.id is a PrincipalRef; its .id is the principal's string id.
            return f"principal:{principal.id.id}"

    host = request.client.host if request.client is not None else "unknown"
    return f"host:{host}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Refuse requests that exceed the configured per-caller window."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: SlidingWindowRateLimiter,
        exempt_paths: Sequence[str] = (),
        key_for: Callable[[Request], str],
    ) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._exempt_paths = frozenset(exempt_paths)
        self._key_for = key_for

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self._exempt_paths:
            return await call_next(request)

        decision = self._limiter.check(self._key_for(request))
        if decision.allowed:
            return await call_next(request)

        # The exception handlers live inside the router, so a middleware cannot raise
        # ApiError and have it rendered with the shared envelope. Calling the handler
        # directly reuses that envelope rather than hand-rolling a JSONResponse.
        _logger.info(
            "rate limit exceeded",
            extra={"request_id": request_id_for(request), "path": request.url.path},
        )
        response = await api_error_handler(request, ApiError(ErrorCode.RATE_LIMITED))
        response.headers["Retry-After"] = str(max(1, math.ceil(decision.retry_after_seconds)))
        return response
