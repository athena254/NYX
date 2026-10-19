"""Repository for idempotency/dedupe records (SPEC §11.2). Never commits implicitly."""

from __future__ import annotations

from sqlalchemy.orm import Session

from nyx.domain.idempotency import IdempotencyRecord
from nyx.storage.repositories._mapping import as_utc
from nyx.storage.tables import IdempotencyRow


class IdempotencyRepository:
    """Persistence operations for :class:`IdempotencyRecord`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find(self, scope: str, key: str) -> IdempotencyRecord | None:
        row = self._session.get(IdempotencyRow, (scope, key))
        return None if row is None else _to_domain(row)

    def add(self, record: IdempotencyRecord) -> IdempotencyRecord:
        self._session.add(
            IdempotencyRow(
                scope=record.scope,
                dedupe_key=record.key,
                record_id=record.record_id,
                request_hash=record.request_hash,
                rationale=record.rationale,
                created_at=record.created_at,
            )
        )
        return record


def _to_domain(row: IdempotencyRow) -> IdempotencyRecord:
    return IdempotencyRecord.model_validate(
        {
            "schema_version": "1.0",
            "scope": row.scope,
            "key": row.dedupe_key,
            "record_id": row.record_id,
            "request_hash": row.request_hash,
            "rationale": row.rationale,
            "created_at": as_utc(row.created_at),
        }
    )
