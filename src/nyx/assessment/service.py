"""Durable assessment scheduling and bounded draining.

This is operational state, deliberately distinct from append-only findings.  It provides
transaction-scoped enqueue/revise, lease-safe bounded draining, and visible backlog state; callers
must wire a drain explicitly and no background worker is started here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from nyx.assessment.tables import AssessmentJobRow
from nyx.domain.base import utc_now
from nyx.domain.canonical import content_hash
from nyx.domain.ids import new_id
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.claims import ClaimRepository
from nyx.storage.repositories.events import EventRepository
from nyx.storage.repositories.evidence import EvidenceRepository


@dataclass(frozen=True)
class AssessmentBacklog:
    queued: int
    leased: int
    dead_letter: int


@dataclass(frozen=True)
class AssessmentJob:
    id: str
    event_id: str
    status: str
    attempt_count: int
    max_attempts: int
    lease_token: str | None


class AssessmentScheduler:
    """Enqueue only accepted inputs, using the caller's still-open transaction."""

    def enqueue_event(self, session: Session, event_id: str) -> None:
        event = EventRepository(session).get(event_id)
        if event is not None:
            self._enqueue(session, event_id, "event_created", event_id, event)

    def enqueue_evidence(self, session: Session, evidence_id: str) -> None:
        evidence = EvidenceRepository(session).get(evidence_id)
        if evidence is None or evidence.relevance_to_claim is None:
            return
        claim = ClaimRepository(session).get(evidence.relevance_to_claim)
        if claim is None:
            return
        for event_id in claim.source_event_ids:
            if EventRepository(session).get(event_id) is not None:
                self._enqueue(session, event_id, "evidence_created", evidence_id, evidence)

    @staticmethod
    def _enqueue(
        session: Session, event_id: str, kind: str, record_id: str, record: object
    ) -> None:
        digest = content_hash(record)
        key = content_hash(
            {"event_id": event_id, "trigger_kind": kind, "trigger_record_id": record_id}
        )
        exists = session.scalar(
            select(AssessmentJobRow.id).where(AssessmentJobRow.dedupe_key == key)
        )
        if exists is not None:
            return
        now = utc_now()
        session.add(
            AssessmentJobRow(
                id=new_id("vbj_"),
                event_id=event_id,
                trigger_kind=kind,
                trigger_record_id=record_id,
                dedupe_key=key,
                input_digest=digest,
                status="queued",
                requested_at=now,
                available_at=now,
                lease_token=None,
                leased_until=None,
                attempt_count=0,
                max_attempts=3,
                last_error_type=None,
                completed_at=None,
                dead_lettered_at=None,
            )
        )


class AssessmentWorker:
    """Explicit bounded at-least-once worker.  No thread/task is created by this class."""

    def __init__(self, session_factory: SessionFactory, *, lease_seconds: float = 30.0) -> None:
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds

    def backlog(self) -> AssessmentBacklog:
        with session_scope(self._session_factory) as session:
            rows = list(session.scalars(select(AssessmentJobRow.status)))
            return AssessmentBacklog(
                rows.count("queued"), rows.count("leased"), rows.count("dead_letter")
            )

    def drain(self, *, limit: int = 10) -> list[str]:
        completed: list[str] = []
        for _ in range(limit):
            claimed = self._claim_one()
            if claimed is None:
                break
            try:
                # Import at execution time to keep ingestion's transaction seam independent of
                # critic package registration during application import.
                from nyx.critics.registry import run_selected
                from nyx.domain.enums import CriticResultStatus

                with session_scope(self._session_factory) as session:
                    outcomes = run_selected(
                        session, event_id=claimed.event_id, assessment_job_id=claimed.id
                    )
                if not outcomes:
                    self._retry_or_dead_letter(claimed, "no_critics_registered")
                elif any(item.result is CriticResultStatus.ERROR for item in outcomes):
                    self._retry_or_dead_letter(claimed, "critic_error")
                else:
                    self._complete(claimed)
                    completed.append(claimed.id)
            except Exception as exc:  # execution failures remain visible operational state
                self._retry_or_dead_letter(claimed, type(exc).__name__)
        return completed

    def _claim_one(self) -> AssessmentJob | None:
        now = utc_now()
        token = str(uuid4())
        with session_scope(self._session_factory) as session:
            due = session.scalar(
                select(AssessmentJobRow)
                .where(
                    or_(
                        and_(
                            AssessmentJobRow.status == "queued",
                            AssessmentJobRow.available_at <= now,
                        ),
                        and_(
                            AssessmentJobRow.status == "leased", AssessmentJobRow.leased_until < now
                        ),
                    )
                )
                .order_by(AssessmentJobRow.available_at, AssessmentJobRow.id)
                .limit(1)
            )
            if due is None:
                return None
            updated = cast(
                CursorResult[Any],
                session.execute(
                    update(AssessmentJobRow)
                    .where(
                        AssessmentJobRow.id == due.id,
                        or_(
                            and_(
                                AssessmentJobRow.status == "queued",
                                AssessmentJobRow.available_at <= now,
                            ),
                            and_(
                                AssessmentJobRow.status == "leased",
                                AssessmentJobRow.leased_until < now,
                            ),
                        ),
                    )
                    .values(
                        status="leased",
                        lease_token=token,
                        leased_until=now + timedelta(seconds=self._lease_seconds),
                        attempt_count=AssessmentJobRow.attempt_count + 1,
                    )
                    .execution_options(synchronize_session=False)
                ),
            )
            if updated.rowcount != 1:
                return None
            return AssessmentJob(
                due.id, due.event_id, "leased", due.attempt_count + 1, due.max_attempts, token
            )

    def _complete(self, job: AssessmentJob) -> None:
        with session_scope(self._session_factory) as session:
            session.execute(
                update(AssessmentJobRow)
                .where(
                    AssessmentJobRow.id == job.id, AssessmentJobRow.lease_token == job.lease_token
                )
                .values(
                    status="completed", completed_at=utc_now(), lease_token=None, leased_until=None
                )
            )

    def _retry_or_dead_letter(self, job: AssessmentJob, error_type: str) -> None:
        now = utc_now()
        with session_scope(self._session_factory) as session:
            row = session.get(AssessmentJobRow, job.id)
            if row is None or row.lease_token != job.lease_token:
                return
            if row.attempt_count >= row.max_attempts:
                row.status, row.dead_lettered_at = "dead_letter", now
            else:
                row.status, row.available_at = (
                    "queued",
                    now + timedelta(seconds=min(60, 2**row.attempt_count)),
                )
            row.last_error_type, row.lease_token, row.leased_until = error_type[:128], None, None
