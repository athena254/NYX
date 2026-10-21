"""The API rate limiter's HTTP contract (SPEC §12): 429, Retry-After, exemptions, isolation."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from starlette.middleware import Middleware

from nyx.api.app import create_app
from nyx.api.rate_limit import (
    RateLimitMiddleware,
    SlidingWindowRateLimiter,
    principal_or_host_key,
)
from nyx.config import Settings
from nyx.storage.db import SessionFactory

_CLIENT_A = ("192.0.2.10", 50000)
_CLIENT_B = ("192.0.2.11", 50000)


def _rate_limited_app(
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
    limiter: SlidingWindowRateLimiter,
) -> FastAPI:
    app = create_app(settings, engine=engine, session_factory=session_factory)
    # Append rather than add: the request-id middleware stays outermost, so denied requests
    # still get a request id and the response header.
    app.user_middleware.append(
        Middleware(
            RateLimitMiddleware,
            limiter=limiter,
            exempt_paths=tuple(settings.rate_limit_exempt_paths),
            key_for=principal_or_host_key,
        )
    )
    return app


def _exhaust(client: TestClient, times: int) -> None:
    for _ in range(times):
        assert client.get("/api/v1/systems").status_code == 200


def test_exhaustion_returns_429_with_retry_after_and_the_shared_envelope(
    settings: Settings, engine: Engine, session_factory: SessionFactory
) -> None:
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60.0)
    app = _rate_limited_app(settings, engine, session_factory, limiter)

    with TestClient(app, client=_CLIENT_A) as client:
        _exhaust(client, 2)

        refused = client.get("/api/v1/systems")

        assert refused.status_code == 429
        assert refused.headers["Retry-After"].isdigit()
        assert int(refused.headers["Retry-After"]) >= 1
        body = refused.json()
        assert body["code"] == "RATE_LIMITED"
        assert body["request_id"] != "-"
        assert refused.headers["X-Request-Id"] == body["request_id"]


def test_health_and_ready_are_exempt_after_exhaustion(
    settings: Settings, engine: Engine, session_factory: SessionFactory
) -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
    app = _rate_limited_app(settings, engine, session_factory, limiter)

    with TestClient(app, client=_CLIENT_A) as client:
        _exhaust(client, 1)
        assert client.get("/api/v1/systems").status_code == 429

        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code == 200


def test_two_client_keys_have_independent_budgets(
    settings: Settings, engine: Engine, session_factory: SessionFactory
) -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
    app = _rate_limited_app(settings, engine, session_factory, limiter)

    with TestClient(app, client=_CLIENT_A) as client_a:
        _exhaust(client_a, 1)
        assert client_a.get("/api/v1/systems").status_code == 429

    with TestClient(app, client=_CLIENT_B) as client_b:
        assert client_b.get("/api/v1/systems").status_code == 200
        assert client_b.get("/api/v1/systems").status_code == 429


def test_refusal_body_contains_no_limiter_internal_state(
    settings: Settings, engine: Engine, session_factory: SessionFactory
) -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
    app = _rate_limited_app(settings, engine, session_factory, limiter)

    with TestClient(app, client=_CLIENT_A) as client:
        _exhaust(client, 1)
        refused = client.get("/api/v1/systems")

        payload = refused.text
        assert set(refused.json()) == {"code", "message", "request_id"}
        for internal in ("timestamps", "deque", "host:192.0.2.10", "window_seconds"):
            assert internal not in payload
