"""Retention classes and expiry (``docs/privacy-and-retention.md`` §4, SPEC §11.3).

A retention *policy* is the deployment's chosen durations per class. The durations are
configuration, not constants: obligations differ by deployment, so a deployment must record its
durations and must not silently exceed them. This module reads them from
:class:`nyx.config.Settings` and answers one question per record: is its payload due for purge?

``legal_hold`` is the one class with **no** duration. The early return in
:meth:`RetentionPolicy.is_expired` is deliberate: it makes a legal hold un-purgable by
construction rather than by a duration lookup that could be reached with the wrong key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from nyx.config import Settings
from nyx.domain.enums import RetentionClass


class HasRetentionClass(Protocol):
    """The minimal shape a record must have to be judged for expiry."""

    retention_class: RetentionClass | str


# Days per class. ``legal_hold`` is absent by design: it has no duration, and an attempt to look
# one up is a programming error rather than a policy decision.
_CLASS_DAYS: dict[RetentionClass, str] = {
    RetentionClass.EPHEMERAL: "ephemeral_days",
    RetentionClass.STANDARD: "standard_days",
    RetentionClass.EXTENDED: "extended_days",
    RetentionClass.DERIVED_METADATA: "derived_metadata_days",
}


@dataclass(frozen=True)
class RetentionPolicy:
    """Deployment retention durations and the expiry decision they imply."""

    ephemeral_days: int
    standard_days: int
    extended_days: int
    derived_metadata_days: int

    @classmethod
    def from_settings(cls, settings: Settings) -> RetentionPolicy:
        """Build a policy from the deployment's ``retention_*`` settings."""

        return cls(
            ephemeral_days=settings.retention_ephemeral_days,
            standard_days=settings.retention_standard_days,
            extended_days=settings.retention_extended_days,
            derived_metadata_days=settings.retention_derived_metadata_days,
        )

    def days_for(self, retention_class: RetentionClass | str) -> int | None:
        """Return the configured duration for ``retention_class``.

        ``None`` means "never expires", which is only ever the answer for ``legal_hold``. Every
        other class has a duration, so ``None`` here is a signal, not a fallback.
        """

        try:
            value = RetentionClass(retention_class)
        except ValueError:
            return None
        if value is RetentionClass.LEGAL_HOLD:
            return None
        return int(getattr(self, _CLASS_DAYS[value]))

    def is_expired(self, record: HasRetentionClass, now: datetime) -> bool:
        """Return whether ``record``'s payload is due for purge at ``now``.

        ``legal_hold`` never expires. The check is an explicit early return, not a duration
        lookup: a hold lasts until an authorised principal releases it, and there is no configured
        duration that could accidentally expire it.
        """

        try:
            retention_class = RetentionClass(record.retention_class)
        except ValueError:
            # An unknown class is not a licence to purge. It is retained until a human
            # classifies it; this is the safe side of an ambiguous record.
            return False
        if retention_class is RetentionClass.LEGAL_HOLD:
            return False
        days = self.days_for(retention_class)
        if days is None:
            return False
        return now >= self._expiry_at(record, days)

    def _expiry_at(self, record: HasRetentionClass, days: int) -> datetime:
        """Return the instant at which a record of this policy expires."""

        return as_utc(self._record_time(record)) + timedelta(days=days)

    @staticmethod
    def _record_time(record: HasRetentionClass) -> datetime:
        """Read the retention start time from a record or row.

        A domain model exposes ``occurred_at``/``captured_at``/``created_at`` as aware datetimes.
        A bare object with only a ``retention_class`` and no clock has no start time to measure
        from, so it is treated as written *now* -- never due, rather than due by default.
        """

        for attribute in ("occurred_at", "captured_at", "created_at", "received_at"):
            value = getattr(record, attribute, None)
            if isinstance(value, datetime):
                return value
        return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime; a naive value is read as UTC, never as local time."""

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
