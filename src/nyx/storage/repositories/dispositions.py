"""Repository for dispositions (SPEC §5.7). Append-only: a disposition supersedes by linking.

There is deliberately no ``update`` and no ``delete``. The current disposition for a finding is the
latest by ``decided_at`` -- derived on read, never stored as a mutable pointer -- and the chain is
reconstructable by following ``supersedes_disposition_id`` backwards from any link.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nyx.disputes.models import Disposition
from nyx.storage.repositories._mapping import as_utc
from nyx.storage.tables import DispositionRow


class DispositionRepository:
    """Persistence operations for ``Disposition``. Never commits implicitly."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, disposition: Disposition) -> Disposition:
        self._session.add(
            DispositionRow(
                id=disposition.id,
                schema_version=disposition.schema_version,
                finding_id=disposition.finding_id,
                outcome=disposition.outcome.value,
                rationale=disposition.rationale,
                decided_by=disposition.decided_by,
                decided_at=disposition.decided_at,
                verification_run_ids=list(disposition.verification_run_ids),
                policy_version=disposition.policy_version,
                supersedes_disposition_id=disposition.supersedes_disposition_id,
            )
        )
        return disposition

    def get(self, disposition_id: str) -> Disposition | None:
        row = self._session.get(DispositionRow, disposition_id)
        return None if row is None else _to_domain(row)

    def list(self, *, finding_id: str, limit: int = 100, offset: int = 0) -> Sequence[Disposition]:
        """Return a finding's dispositions oldest-first, i.e. in chain order."""

        statement = (
            select(DispositionRow)
            .where(DispositionRow.finding_id == finding_id)
            .order_by(DispositionRow.decided_at, DispositionRow.id)
            .limit(limit)
            .offset(offset)
        )
        return [_to_domain(row) for row in self._session.scalars(statement)]

    def latest(self, finding_id: str) -> Disposition | None:
        """Return the finding's current disposition: the newest recorded one.

        Ties on ``decided_at`` break on ``id``, which is lexicographically ordered by creation time,
        so the result is deterministic even when two dispositions share a timestamp.
        """

        statement = (
            select(DispositionRow)
            .where(DispositionRow.finding_id == finding_id)
            .order_by(DispositionRow.decided_at.desc(), DispositionRow.id.desc())
            .limit(1)
        )
        row = self._session.scalars(statement).first()
        return None if row is None else _to_domain(row)

    def chain(self, disposition_id: str) -> Sequence[Disposition]:
        """Return the full supersession chain, oldest first, ending at ``disposition_id``.

        Follows ``supersedes_disposition_id`` backwards. Because a disposition may be superseded at
        most once (enforced by a unique constraint), the chain is linear and this walk terminates.
        """

        links: list[Disposition] = []
        current: str | None = disposition_id
        seen: set[str] = set()
        while current is not None:
            # A cycle would mean a corrupted chain; stop rather than loop forever.
            if current in seen:
                break
            seen.add(current)
            row = self._session.get(DispositionRow, current)
            if row is None:
                break
            links.append(_to_domain(row))
            current = row.supersedes_disposition_id
        links.reverse()
        return links

    def count_for_finding(self, finding_id: str) -> int:
        statement = (
            select(func.count())
            .select_from(DispositionRow)
            .where(DispositionRow.finding_id == finding_id)
        )
        return int(self._session.scalar(statement) or 0)


def _to_domain(row: DispositionRow) -> Disposition:
    return Disposition.model_validate(
        {
            "id": row.id,
            "schema_version": row.schema_version,
            "finding_id": row.finding_id,
            "outcome": row.outcome,
            "rationale": row.rationale,
            "decided_by": row.decided_by,
            "decided_at": as_utc(row.decided_at),
            "verification_run_ids": row.verification_run_ids,
            "policy_version": row.policy_version,
            "supersedes_disposition_id": row.supersedes_disposition_id,
        }
    )
