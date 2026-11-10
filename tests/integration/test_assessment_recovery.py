"""Recovery behaviour for durable assessment jobs.

The queue is deliberately explicit: these tests reconstruct the service and worker over the
same migrated SQLite database rather than relying on in-process state or a background task.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select

from nyx.assessment.service import AssessmentWorker
from nyx.assessment.tables import AssessmentJobRow
from nyx.config import Settings
from nyx.critics import registry as critic_registry
from nyx.critics.base import CriticResult, CriticScope
from nyx.domain.enums import CriticResultStatus, CriticType
from nyx.ingestion.service import IngestionService
from nyx.storage.db import (
    SessionFactory,
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from nyx.storage.tables import CriticRunRow


class _NoFindingCritic:
    critic_id = "test-assessment-recovery"
    critic_version = "1"
    critic_type = CriticType.DETERMINISTIC
    description = "Test-only deterministic critic that records a completed assessment."
    scope = CriticScope(can_assess=["test recovery"], cannot_assess=[])

    def run(self, context: object) -> CriticResult:
        return CriticResult(
            critic_id=self.critic_id,
            critic_version=self.critic_version,
            result=CriticResultStatus.NO_FINDING,
            proposition="The test recovery critic found no issue in its narrow scope.",
            scope=context.scope,  # type: ignore[union-attr]
            observations=["The persisted event was available after reopening the database."],
            rationale="This deterministic fixture only proves the queue recovery boundary.",
            recommended_next_step="No action is required; this is not evidence the event is true.",
        )


class _ErrorCritic(_NoFindingCritic):
    critic_id = "test-assessment-error"

    def run(self, context: object) -> CriticResult:
        return CriticResult(
            critic_id=self.critic_id,
            critic_version=self.critic_version,
            result=CriticResultStatus.ERROR,
            proposition="The deterministic test critic could not complete its assessment.",
            scope=context.scope,  # type: ignore[union-attr]
            observations=["The test critic intentionally returned an operational error."],
            rationale=(
                "The queue must retain a critic error for bounded retry rather than complete it."
            ),
            recommended_next_step="Inspect the persisted critic error before retrying.",
        )


@pytest.fixture()
def isolated_critic_registry() -> Iterator[None]:
    """Keep these explicit worker tests independent from the process-global critic pack."""

    original = dict(critic_registry._REGISTRY)
    critic_registry._REGISTRY.clear()
    try:
        yield
    finally:
        critic_registry._REGISTRY.clear()
        critic_registry._REGISTRY.update(original)


def _accepted_event(service: IngestionService) -> str:
    system = service.register_system(
        {
            "name": "assessment-recovery-system",
            "kind": "agent",
            "environment": "development",
            "owner": "test",
            "adapter": "http",
        },
        request_id="assessment-recovery-system",
    )
    assert system.status == "created", system.error
    assert system.record_id is not None

    event = service.ingest_event(
        {
            "source_system_id": system.record_id,
            "event_type": "agent.completed",
            "occurred_at": "2026-10-27T12:00:00Z",
            "correlation_id": "assessment-recovery-correlation",
            "payload": {"artifact": "queue-recovery"},
            "producer_event_id": "assessment-recovery-event",
            "trust_class": "observed",
            "labels": ["assessment-recovery"],
            "retention_class": "standard",
        },
        request_id="assessment-recovery-event",
    )
    assert event.status == "created", event.error
    assert event.record_id is not None
    return event.record_id


def _reopened_factory(settings: Settings) -> tuple[SessionFactory, object]:
    engine = create_engine_from_settings(settings)
    return create_session_factory(engine), engine


def test_accepted_event_survives_reconstruction_and_second_drain_does_not_duplicate_runs(
    service: IngestionService,
    settings: Settings,
    session_factory: SessionFactory,
    isolated_critic_registry: None,
) -> None:
    critic_registry.register(_NoFindingCritic())
    event_id = _accepted_event(service)

    reopened_factory, reopened_engine = _reopened_factory(settings)
    try:
        reconstructed_service = IngestionService(reopened_factory, settings)
        assert reconstructed_service.get_event(event_id) is not None

        worker = AssessmentWorker(reopened_factory)
        assert worker.backlog().queued == 1
        completed_once = worker.drain(limit=1)
        completed_twice = worker.drain(limit=1)

        assert len(completed_once) == 1
        assert completed_twice == []
    finally:
        reopened_engine.dispose()  # type: ignore[union-attr]

    with session_scope(session_factory) as session:
        jobs = list(session.scalars(select(AssessmentJobRow)))
        runs = list(session.scalars(select(CriticRunRow)))

    assert [(job.id, job.status, job.attempt_count) for job in jobs] == [
        (completed_once[0], "completed", 1)
    ]
    assert [(run.assessment_job_id, run.critic_id) for run in runs] == [
        (completed_once[0], _NoFindingCritic.critic_id)
    ]


def test_worker_never_marks_a_job_completed_when_no_critic_runs(
    service: IngestionService,
    session_factory: SessionFactory,
    isolated_critic_registry: None,
) -> None:
    _accepted_event(service)

    worker = AssessmentWorker(session_factory)
    assert worker.drain(limit=1) == []

    with session_scope(session_factory) as session:
        job = session.scalar(select(AssessmentJobRow))
        runs = list(session.scalars(select(CriticRunRow)))

    assert job is not None
    assert job.status == "queued"
    assert job.attempt_count == 1
    assert job.last_error_type == "no_critics_registered"
    assert runs == []


def test_critic_error_is_retried_instead_of_silently_completing(
    service: IngestionService,
    session_factory: SessionFactory,
    isolated_critic_registry: None,
) -> None:
    critic_registry.register(_ErrorCritic())
    _accepted_event(service)

    assert AssessmentWorker(session_factory).drain(limit=1) == []

    with session_scope(session_factory) as session:
        job = session.scalar(select(AssessmentJobRow))
        runs = list(session.scalars(select(CriticRunRow)))

    assert job is not None
    assert (job.status, job.attempt_count, job.last_error_type) == ("queued", 1, "critic_error")
    assert [(run.assessment_job_id, run.critic_id, run.result) for run in runs] == [
        (job.id, _ErrorCritic.critic_id, "error")
    ]
