"""Retention policy: per-class expiry, the exact boundary, and the legal-hold wall.

``docs/privacy-and-retention.md`` §4 fixes the classes and their intent, not a number; the
durations are deployment configuration. These tests pin the *decision* the policy makes, so a
change to a default duration or to the class mapping shows up here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from nyx.config import Settings
from nyx.domain.enums import RetentionClass
from nyx.retention.policy import RetentionPolicy

_NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
_FAR_FUTURE = datetime(2226, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class _Record:
    """The minimal shape :meth:`RetentionPolicy.is_expired` reads."""

    retention_class: object
    created_at: datetime = _NOW


@pytest.fixture()
def policy() -> RetentionPolicy:
    return RetentionPolicy(
        ephemeral_days=7,
        standard_days=90,
        extended_days=365,
        derived_metadata_days=730,
    )


# -- per-class expiry -----------------------------------------------------------


def test_each_class_expires_after_its_own_duration(policy: RetentionPolicy) -> None:
    cases = [
        (RetentionClass.EPHEMERAL, timedelta(days=7)),
        (RetentionClass.STANDARD, timedelta(days=90)),
        (RetentionClass.EXTENDED, timedelta(days=365)),
        (RetentionClass.DERIVED_METADATA, timedelta(days=730)),
    ]

    for retention_class, duration in cases:
        record = _Record(retention_class, created_at=_NOW - duration - timedelta(seconds=1))
        assert policy.is_expired(record, _NOW), f"{retention_class} should be expired"

        fresh = _Record(retention_class, created_at=_NOW - duration + timedelta(seconds=1))
        assert not policy.is_expired(fresh, _NOW), f"{retention_class} should be retained"


def test_policy_reads_its_durations_from_settings() -> None:
    settings = Settings(
        retention_ephemeral_days=1,
        retention_standard_days=30,
        retention_extended_days=180,
        retention_derived_metadata_days=365,
    )
    policy = RetentionPolicy.from_settings(settings)

    assert (policy.ephemeral_days, policy.standard_days) == (1, 30)
    assert (policy.extended_days, policy.derived_metadata_days) == (180, 365)


# -- the exact boundary ---------------------------------------------------------


@pytest.mark.parametrize(
    "retention_class",
    [
        RetentionClass.EPHEMERAL,
        RetentionClass.STANDARD,
        RetentionClass.EXTENDED,
        RetentionClass.DERIVED_METADATA,
    ],
)
def test_a_record_expires_at_exactly_its_duration(
    policy: RetentionPolicy, retention_class: RetentionClass
) -> None:
    days = policy.days_for(retention_class)
    assert days is not None

    record = _Record(retention_class, created_at=_NOW - timedelta(days=days))

    assert policy.is_expired(record, _NOW)
    assert not policy.is_expired(record, _NOW - timedelta(microseconds=1))


# -- legal_hold never expires ---------------------------------------------------


@pytest.mark.parametrize(
    "now",
    [
        _NOW,
        _NOW + timedelta(days=365),
        _NOW + timedelta(days=3650),
        _FAR_FUTURE,
    ],
)
def test_legal_hold_never_expires(policy: RetentionPolicy, now: datetime) -> None:
    record = _Record(RetentionClass.LEGAL_HOLD, created_at=_NOW - timedelta(days=3650))

    assert not policy.is_expired(record, now)
    assert policy.days_for(RetentionClass.LEGAL_HOLD) is None


def test_legal_hold_is_not_expired_even_when_a_duration_is_configured_for_it(
    policy: RetentionPolicy,
) -> None:
    """The early return is what protects a hold, not the absence of a configured duration."""

    record = _Record(RetentionClass.LEGAL_HOLD, created_at=datetime(2000, 1, 1, tzinfo=UTC))

    assert not policy.is_expired(record, _FAR_FUTURE)
    assert not RetentionPolicy(
        ephemeral_days=1,
        standard_days=1,
        extended_days=1,
        derived_metadata_days=1,
    ).is_expired(record, _FAR_FUTURE)


# -- a record with no retention class ------------------------------------------


def test_a_record_with_no_retention_class_is_not_expired(policy: RetentionPolicy) -> None:
    assert not policy.is_expired(_Record(""), _NOW)
    assert not policy.is_expired(_Record("not_a_class"), _NOW)
    assert policy.days_for("not_a_class") is None


def test_a_record_with_no_clock_is_not_expired(policy: RetentionPolicy) -> None:
    """Nothing to measure from, so it is retained rather than purged by default."""

    @dataclass(frozen=True)
    class _NoClock:
        retention_class: object = RetentionClass.STANDARD

    assert not policy.is_expired(_NoClock(), _NOW)


# -- string values, as the database stores them --------------------------------


def test_a_record_may_carry_its_class_as_a_plain_string(policy: RetentionPolicy) -> None:
    assert not policy.is_expired(_Record("standard", created_at=_NOW), _NOW)
    assert policy.is_expired(_Record("standard", created_at=_NOW - timedelta(days=91)), _NOW)
