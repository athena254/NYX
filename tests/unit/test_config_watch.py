"""The watcher's fail-closed configuration (ADR 0009).

A watcher that is switched on but has nothing to watch is a misconfiguration, not a degraded
state: it would otherwise run, fetch nothing, and report success. These tests pin the refusal
and, just as importantly, pin the default being OFF.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nyx.config import Settings


def _watch(**overrides: object) -> dict[str, object]:
    """A valid watch configuration, so each test states only what it is testing."""

    values: dict[str, object] = {
        "enabled_adapters": ["fixture"],
        "git_ci_provider": "fixture",
        "git_ci_repository": "example/repo",
        "watch_enabled": True,
        "watch_ref": "main",
    }
    values.update(overrides)
    return values


def test_watch_is_off_by_default() -> None:
    """The watcher must never be on unless a deployment switches it on."""

    settings = Settings()

    assert settings.watch_enabled is False
    assert settings.watch_interval_seconds > 0
    assert settings.watch_ref


def test_watch_accepts_a_complete_configuration() -> None:
    settings = Settings(**_watch())

    assert settings.watch_enabled is True
    assert settings.watch_ref == "main"


def test_watch_refuses_an_empty_target_ref() -> None:
    """A watcher with no ref would fetch nothing and report success."""

    with pytest.raises(ValidationError, match="NYX_WATCH_REF is empty"):
        Settings(**_watch(watch_ref="   "))


def test_watch_refuses_no_enabled_adapter() -> None:
    """A watcher with no adapter has nothing to observe."""

    with pytest.raises(ValidationError, match="NYX_ENABLED_ADAPTERS is empty"):
        Settings(**_watch(enabled_adapters=[]))


def test_watch_off_needs_no_adapter() -> None:
    """The refusal is conditional: with the watcher off, no adapter is required.

    A setting that demanded an adapter even when the feature is disabled would make the watcher
    a prerequisite for running NYX at all, which ADR 0008 and ADR 0009 both forbid.
    """

    settings = Settings(watch_enabled=False)

    assert settings.enabled_adapters == []


def test_watch_interval_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(**_watch(watch_interval_seconds=0))
