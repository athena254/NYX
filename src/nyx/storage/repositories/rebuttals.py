"""Repository for rebuttals (SPEC §5.7). Append-only: no update or delete path exists."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.disputes.models import Rebuttal
from nyx.storage.repositories._mapping import as_utc
from nyx.storage.tables import RebuttalRow


class RebuttalRepository:
    """Persistence operations for ``Rebuttal``. Never commits implicitly."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, rebuttal: Rebuttal) -> Rebuttal:
        self._session.add(
            RebuttalRow(
                id=rebuttal.id,
                schema_version=rebuttal.schema_version,
                finding_id=rebuttal.finding_id,
                submitted_by=rebuttal.submitted_by,
                submitted_at=rebuttal.submitted_at,
                argument=rebuttal.argument,
                evidence_ids=list(rebuttal.evidence_ids),
                requested_outcome=rebuttal.requested_outcome.value,
            )
        )
        return rebuttal

    def get(self, rebuttal_id: str) -> Rebuttal | None:
        row = self._session.get(RebuttalRow, rebuttal_id)
        return None if row is None else _to_domain(row)

    def list(self, *, finding_id: str, limit: int = 100, offset: int = 0) -> list[Rebuttal]:
        """Return a finding's rebuttals oldest-first, so history reads in the order it happened."""

        statement = (
            select(RebuttalRow)
            .where(RebuttalRow.finding_id == finding_id)
            .order_by(RebuttalRow.submitted_at, RebuttalRow.id)
            .limit(limit)
            .offset(offset)
        )
        return [_to_domain(row) for row in self._session.scalars(statement)]


def _to_domain(row: RebuttalRow) -> Rebuttal:
    return Rebuttal.model_validate(
        {
            "id": row.id,
            "schema_version": row.schema_version,
            "finding_id": row.finding_id,
            "submitted_by": row.submitted_by,
            "submitted_at": as_utc(row.submitted_at),
            "argument": row.argument,
            "evidence_ids": row.evidence_ids,
            "requested_outcome": row.requested_outcome,
        }
    )
