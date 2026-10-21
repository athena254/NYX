"""Sliding-window rate-limit math with an injected clock (SPEC §12)."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from starlette.datastructures import Headers

from nyx.api.rate_limit import (
    RateLimitDecision,
    SlidingWindowRateLimiter,
    principal_or_host_key,
)
from nyx.config import Settings


def _clock(start: float = 0.0) -> tuple[list[float], Callable[[], float]]:
    now = [start]
    return now, lambda: now[0]


def test_allowed_hits_count_down_and_exhaustion_refuses() -> None:
    _, clock = _clock()
    limiter = SlidingWindowRateLimiter(limit=3, window_seconds=60.0, clock=clock)

    first = limiter.check("caller")
    second = limiter.check("caller")
    third = limiter.check("caller")
    refused = limiter.check("caller")

    assert (first.allowed, first.remaining) == (True, 2)
    assert (second.allowed, second.remaining) == (True, 1)
    assert (third.allowed, third.remaining) == (True, 0)
    assert refused.allowed is False
    assert refused.limit == 3
    assert refused.remaining == 0
    assert refused.retry_after_seconds == pytest.approx(60.0)


def test_burst_straddling_the_window_boundary_cannot_double_the_allowance() -> None:
    now, clock = _clock()
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10.0, clock=clock)

    now[0] = 9.9
    assert limiter.check("caller").allowed is True
    assert limiter.check("caller").allowed is True

    # A fixed window would reset at 10.0 and allow another full burst; a sliding one does not.
    now[0] = 10.0
    denied = limiter.check("caller")
    assert denied.allowed is False

    now[0] = 19.7
    assert limiter.check("caller").allowed is False

    now[0] = 20.0
    assert limiter.check("caller").allowed is True


def test_retry_after_is_positive_bounded_and_closes_the_window() -> None:
    now, clock = _clock()
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0, clock=clock)
    assert limiter.check("caller").allowed is True

    for elapsed in (0.0, 30.0, 59.9):
        now[0] = elapsed
        decision = limiter.check("caller")
        assert decision.allowed is False
        assert 0.0 < decision.retry_after_seconds <= 60.0

    now[0] = 60.0
    assert limiter.check("caller").allowed is True


def test_refused_hits_do_not_extend_the_window() -> None:
    now, clock = _clock()
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=10.0, clock=clock)
    assert limiter.check("caller").allowed is True

    now[0] = 9.0
    assert limiter.check("caller").allowed is False
    now[0] = 10.0
    assert limiter.check("caller").allowed is True


def test_reset_forgets_every_caller() -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)
    assert limiter.check("caller").allowed is True
    assert limiter.check("caller").allowed is False

    limiter.reset()

    decision = limiter.check("caller")
    assert decision.allowed is True
    assert decision.remaining == 0


def test_two_keys_have_independent_budgets() -> None:
    limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60.0)

    assert limiter.check("principal:a").allowed is True
    assert limiter.check("principal:a").allowed is False
    assert limiter.check("principal:b").allowed is True
    assert limiter.check("principal:b").allowed is False


def test_constructor_rejects_impossible_configuration() -> None:
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(limit=0, window_seconds=60.0)
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(limit=1, window_seconds=0.0)


def _request(
    settings: Settings, *, headers: dict[str, str], host: str = "203.0.113.7"
) -> SimpleNamespace:
    """A fake request carrying a real settings object, as middleware receives it.

    ``Headers`` rather than a plain dict: Starlette headers are case-insensitive and the auth
    dependency looks the credential up in lowercase, so a plain dict would silently test a
    different code path than production.
    """

    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
        state=SimpleNamespace(),
        client=SimpleNamespace(host=host),
        headers=Headers(headers),
    )


def test_key_uses_the_authenticated_principal_and_ignores_caller_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key comes from the credential, never from a header the caller controls."""

    monkeypatch.setenv("NYX_PROBE_RATE_KEY", "probe-key-value")
    settings = Settings(
        auth_mode="api_key",
        auth_principals=[
            {
                "principal_id": "operator",
                "api_key_env": "NYX_PROBE_RATE_KEY",
                "capabilities": ["findings.disposition"],
            }
        ],
    )
    request = _request(
        settings,
        headers={
            "Authorization": "Bearer probe-key-value",
            "X-Forwarded-For": "198.51.100.9",
            "X-API-Key": "attacker-supplied",
        },
    )

    assert principal_or_host_key(request) == "principal:operator"


def test_key_falls_back_to_client_host_when_unauthenticated() -> None:
    """An unknown caller is limited by host, not let through."""

    settings = Settings(auth_mode="api_key", auth_api_key=SecretStr("operator-key"))
    request = _request(settings, headers={"X-Forwarded-For": "198.51.100.9"})

    assert principal_or_host_key(request) == "host:203.0.113.7"


def test_key_uses_the_host_in_local_mode_even_with_a_credential() -> None:
    """In auth_mode='none' every caller is the same local operator, so a principal key would make
    the whole deployment share one budget. The host key keeps per-caller limits."""

    request = _request(
        Settings(auth_mode="none"),
        headers={"Authorization": "Bearer whatever"},
    )

    assert principal_or_host_key(request) == "host:203.0.113.7"


def test_decision_is_frozen() -> None:
    decision = RateLimitDecision(allowed=True, limit=1, remaining=0, retry_after_seconds=0.0)

    with pytest.raises(AttributeError):
        decision.allowed = False  # type: ignore[misc]
