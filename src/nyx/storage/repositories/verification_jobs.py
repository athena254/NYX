"""Repository for independent verification jobs (SPEC §5.7, §7 step 8).

Append-only: a job's ``status`` and ``outcome`` are recorded facts about an attempt that happened,
not mutable state. A re-run is a new job, never an edit of an old one, so a failed attempt stays
visible next to any later success.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.disputes.models import VerificationJob
from nyx.storage.repositories._mapping import as_utc
from nyx.storage.tables import VerificationJobRow


class VerificationJobRepository:
    """Persistence operations for ``VerificationJob``. Never commits implicitly."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, job: VerificationJob) -> VerificationJob:
        self._session.add(_to_row(job))
        return job

    def get(self, job_id: str) -> VerificationJob | None:
        row = self._session.get(VerificationJobRow, job_id)
        return None if row is None else _to_domain(row)

    def list(
        self,
        *,
        finding_id: str | None = None,
        check_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[VerificationJob]:
        statement = select(VerificationJobRow)
        if finding_id is not None:
            statement = statement.where(VerificationJobRow.finding_id == finding_id)
        if check_id is not None:
            statement = statement.where(VerificationJobRow.check_id == check_id)
        if status is not None:
            statement = statement.where(VerificationJobRow.status == status)
        statement = statement.order_by(VerificationJobRow.requested_at, VerificationJobRow.id)
        statement = statement.limit(limit).offset(offset)
        return [_to_domain(row) for row in self._session.scalars(statement)]

    def list_queued(self, *, limit: int = 100) -> Sequence[VerificationJob]:
        """Return jobs waiting to run, oldest first."""

        statement = (
            select(VerificationJobRow)
            .where(VerificationJobRow.status == "queued")
            .order_by(VerificationJobRow.requested_at, VerificationJobRow.id)
            .limit(limit)
        )
        return [_to_domain(row) for row in self._session.scalars(statement)]


def _to_row(job: VerificationJob) -> VerificationJobRow:
    return VerificationJobRow(
        id=job.id,
        finding_id=job.finding_id,
        check_id=job.check_id,
        requested_by=job.requested_by,
        requested_at=job.requested_at,
        status=job.status.value,
        outcome=None if job.outcome is None else job.outcome.value,
        result_summary=job.result_summary,
        independence=job.independence,
        evidence_ids=list(job.evidence_ids),
        tool_run_refs=list(job.tool_run_refs),
        failure_details=job.failure_details,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def _to_domain(row: VerificationJobRow) -> VerificationJob:
    return VerificationJob.model_validate(
        {
            "id": row.id,
            "finding_id": row.finding_id,
            "check_id": row.check_id,
            "requested_by": row.requested_by,
            "requested_at": as_utc(row.requested_at),
            "status": row.status,
            "outcome": row.outcome,
            "result_summary": row.result_summary,
            "independence": row.independence,
            "evidence_ids": row.evidence_ids,
            "tool_run_refs": row.tool_run_refs,
            "failure_details": row.failure_details,
            "started_at": None if row.started_at is None else as_utc(row.started_at),
            "completed_at": None if row.completed_at is None else as_utc(row.completed_at),
        }
    )
