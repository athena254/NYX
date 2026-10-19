"""Append-only audit writer with a local hash chain.

Each entry records actor, action, target type/id, timestamp, request/correlation id, outcome,
reason, and ``prior_entry_hash``, which links to the previous entry's ``entry_hash``.

Trust limits
------------
The chain detects accidental edits, partial writes, and out-of-order mutation *within the
stored sequence*. It does **not** defeat a privileged attacker who can rewrite the whole
database and recompute every hash. It is not a signature, not a transparency log, and not a
claim of tamper-proof storage (SPEC §5.8, ADR 0003).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints
from sqlalchemy.orm import Session

from nyx.domain.audit import AuditEntry, AuditTarget
from nyx.domain.base import (
    ActionName,
    Identifier,
    NyxModel,
    PrincipalRef,
    SchemaVersion,
    UtcDatetime,
    format_utc,
    utc_now,
)
from nyx.domain.canonical import content_hash
from nyx.domain.enums import AuditOutcome
from nyx.storage.repositories.audit import AuditRepository


class AuditEntryInput(NyxModel):
    """The caller-supplied portion of an audit entry."""

    schema_version: SchemaVersion = "1.0"
    actor: PrincipalRef
    action: ActionName
    target_type: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=64,
            pattern=r"^[a-z][a-z0-9_]*$",
        ),
    ]
    target_id: Identifier
    request_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    correlation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    outcome: AuditOutcome = AuditOutcome.SUCCESS
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    timestamp: UtcDatetime = Field(default_factory=utc_now)


class AuditWriter:
    """Append entries to the audit repository inside the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self._repository = AuditRepository(session)

    def append(self, entry: AuditEntryInput) -> AuditEntry:
        previous = self._repository.last()
        sequence = 1 if previous is None else previous.sequence + 1
        prior_hash = None if previous is None else previous.entry_hash
        entry_hash = _entry_hash(
            schema_version=entry.schema_version,
            sequence=sequence,
            actor=entry.actor,
            action=entry.action,
            target=AuditTarget(type=entry.target_type, id=entry.target_id),
            timestamp=entry.timestamp,
            request_id=entry.request_id,
            correlation_id=entry.correlation_id,
            outcome=entry.outcome,
            reason=entry.reason,
            prior_entry_hash=prior_hash,
        )
        record = AuditEntry(
            schema_version=entry.schema_version,
            sequence=sequence,
            actor=entry.actor,
            action=entry.action,
            target=AuditTarget(type=entry.target_type, id=entry.target_id),
            timestamp=entry.timestamp,
            request_id=entry.request_id,
            correlation_id=entry.correlation_id,
            outcome=entry.outcome,
            reason=entry.reason,
            prior_entry_hash=prior_hash,
            entry_hash=entry_hash,
        )
        return self._repository.add(record)


def _entry_hash(
    *,
    schema_version: str,
    sequence: int,
    actor: PrincipalRef,
    action: str,
    target: AuditTarget,
    timestamp: datetime,
    request_id: str,
    correlation_id: str | None,
    outcome: AuditOutcome,
    reason: str,
    prior_entry_hash: str | None,
) -> str:
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "sequence": sequence,
        "actor": actor.model_dump(mode="json", by_alias=True),
        "action": action,
        "target": target.model_dump(mode="json", by_alias=True),
        "timestamp": format_utc(timestamp),
        "request_id": request_id,
        "correlation_id": correlation_id,
        "outcome": outcome.value,
        "reason": reason,
        "prior_entry_hash": prior_entry_hash,
    }
    return content_hash(payload)
