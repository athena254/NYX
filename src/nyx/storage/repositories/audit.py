"""Repository for append-only audit entries."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.domain.audit import AuditEntry, AuditTarget
from nyx.storage.repositories._mapping import as_json, as_utc
from nyx.storage.tables import AuditRow


class AuditRepository:
    """Persistence operations for ``AuditEntry``. Never commits implicitly.

    There is intentionally no update or delete method. Corrections are new entries.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, entry: AuditEntry) -> AuditEntry:
        row = AuditRow(
            id=entry.id,
            schema_version=entry.schema_version,
            sequence=entry.sequence,
            actor=as_json(entry.actor),
            action=entry.action,
            target_type=entry.target.type,
            target_id=entry.target.id,
            timestamp=entry.timestamp,
            request_id=entry.request_id,
            correlation_id=entry.correlation_id,
            outcome=entry.outcome,
            reason=entry.reason,
            prior_entry_hash=entry.prior_entry_hash,
            entry_hash=entry.entry_hash,
        )
        self._session.add(row)
        return entry

    def get(self, entry_id: str) -> AuditEntry | None:
        row = self._session.get(AuditRow, entry_id)
        return None if row is None else _to_domain(row)

    def list(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        action: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEntry]:
        statement = select(AuditRow)
        if target_type is not None:
            statement = statement.where(AuditRow.target_type == target_type)
        if target_id is not None:
            statement = statement.where(AuditRow.target_id == target_id)
        if action is not None:
            statement = statement.where(AuditRow.action == action)
        statement = statement.order_by(AuditRow.sequence).limit(limit).offset(offset)
        return [_to_domain(row) for row in self._session.scalars(statement)]

    def last(self) -> AuditEntry | None:
        statement = select(AuditRow).order_by(AuditRow.sequence.desc()).limit(1)
        row = self._session.scalars(statement).first()
        return None if row is None else _to_domain(row)

    def find_by_producer_id(self, producer_id: str) -> AuditEntry | None:
        """Look up by the request/correlation identifier supplied by the caller."""

        statement = (
            select(AuditRow)
            .where(AuditRow.request_id == producer_id)
            .order_by(AuditRow.sequence)
            .limit(1)
        )
        row = self._session.scalars(statement).first()
        return None if row is None else _to_domain(row)


def _to_domain(row: AuditRow) -> AuditEntry:
    return AuditEntry.model_validate(
        {
            "id": row.id,
            "schema_version": row.schema_version,
            "sequence": row.sequence,
            "actor": row.actor,
            "action": row.action,
            "target": AuditTarget(type=row.target_type, id=row.target_id),
            "timestamp": as_utc(row.timestamp),
            "request_id": row.request_id,
            "correlation_id": row.correlation_id,
            "outcome": row.outcome,
            "reason": row.reason,
            "prior_entry_hash": row.prior_entry_hash,
            "entry_hash": row.entry_hash,
        }
    )
