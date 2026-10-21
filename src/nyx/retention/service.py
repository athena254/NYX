"""Class-bounded purge (``docs/privacy-and-retention.md`` §4, SPEC §11.3).

Purge is **destructive**, so it is opt-in: ``retention_purge_enabled`` defaults to ``False`` and
only the CLI may set it. This module does not read the flag -- a service that cannot decide for
itself whether it is allowed to destroy content is the safer service. The orchestrator checks the
flag and refuses to call it.

What purge does
---------------
It *tombstones* payload content on the three redactable record classes (``events``, ``claims``,
``evidence_items``). It never deletes a row: the record's identity and its relationships survive,
which is what keeps a disposition that relied on the evidence inspectable (ADR 0003,
privacy-and-retention §5). ``legal_hold`` rows are never purged; the report counts them as
``skipped_legal_hold``.

``dry_run=True`` writes nothing -- not even an audit entry, because an audit entry is a write.
``purged`` then reports what *would* have been purged, which is the only safe way to learn the
blast radius of a purge before committing to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.audit.writer import AuditEntryInput, AuditWriter
from nyx.domain.base import PrincipalRef
from nyx.domain.enums import AuditOutcome, RetentionClass
from nyx.domain.ids import new_request_id
from nyx.retention.policy import RetentionPolicy
from nyx.retention.redaction import _ROW_TYPES, apply_tombstone, current_payload
from nyx.retention.tombstone import tombstone_payload

# Purge does not commit: the caller owns the transaction boundary, exactly as the redaction
# service does.


@dataclass(frozen=True)
class PurgeReport:
    """What a purge pass examined and changed (or would have changed)."""

    scanned: int
    purged: int
    skipped_legal_hold: int
    dry_run: bool


class RetentionService:
    """Tombstone every due payload in the redactable record classes."""

    def __init__(self, session: Session, *, policy: RetentionPolicy, actor: PrincipalRef) -> None:
        self._session = session
        self._policy = policy
        self._actor = actor

    def purge_due(self, *, now: datetime, dry_run: bool) -> PurgeReport:
        """Purge (or report) every due record.

        ``now`` is injected rather than read from the clock, so a caller can purge against a
        deterministic instant and a test can prove the boundary.
        """

        scanned = 0
        purged = 0
        skipped = 0

        for target_type in sorted(_ROW_TYPES):
            for row in self._rows(_ROW_TYPES[target_type]):
                scanned += 1
                if str(getattr(row, "retention_class", "")) == RetentionClass.LEGAL_HOLD:
                    skipped += 1
                    continue
                if not self._policy.is_expired(row, now):
                    continue
                if not dry_run:
                    self._tombstone(target_type, row, now)
                purged += 1

        return PurgeReport(
            scanned=scanned,
            purged=purged,
            skipped_legal_hold=skipped,
            dry_run=dry_run,
        )

    def _rows(self, row_type: type[Any]) -> list[Any]:
        """Read every row of one redactable table."""

        return list(self._session.scalars(select(row_type)))

    def _tombstone(self, target_type: str, row: Any, now: datetime) -> None:
        """Tombstone one row and append the audit entry that preserves the fact."""

        reason = (
            f"retention policy expired {row.retention_class} content; "
            "payload replaced with a tombstone"
        )
        apply_tombstone(
            self._session,
            target_type=target_type,
            record_id=row.id,
            action="payload_redacted",
            reason=reason,
            actor_id=self._actor.id,
            at=now,
            tombstone=tombstone_payload(
                current_payload(row),
                action="payload_redacted",
                actor=self._actor.id,
                reason=reason,
            ),
        )
        AuditWriter(self._session).append(
            AuditEntryInput(
                actor=self._actor,
                action="retention.payload_redacted",
                target_type=target_type,
                target_id=row.id,
                request_id=new_request_id(),
                outcome=AuditOutcome.SUCCESS,
                reason=reason,
            )
        )


__all__ = ["PurgeReport", "RetentionService"]
