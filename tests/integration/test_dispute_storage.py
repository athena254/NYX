"""Persistence round-trip for the dispute tables (SPEC §5.7).

These tests exist because the dispute records are append-only by contract, and a round-trip that
silently dropped a field would still "work" for every other test in the suite. Each record is
written, read back through its repository, and compared field by field.

They also cover the two structural invariants the repositories depend on:

* a disposition chain is linear and reconstructable in order, and a disposition can be superseded at
  most once (the ``uq_dispositions_superseded_once`` constraint);
* registering the same policy version twice is refused by ``uq_policy_versions_rule_version``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from nyx.disputes.models import (
    Disposition,
    PolicyVersion,
    Rebuttal,
    VerificationJob,
)
from nyx.domain.enums import (
    DispositionOutcome,
    RebuttalRequestedOutcome,
    VerificationJobStatus,
    VerificationOutcome,
)
from nyx.storage.db import session_scope
from nyx.storage.repositories.dispositions import DispositionRepository
from nyx.storage.repositories.policy_versions import PolicyVersionRepository
from nyx.storage.repositories.rebuttals import RebuttalRepository
from nyx.storage.repositories.verification_jobs import VerificationJobRepository

_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + "b" * 64


def _rebuttal(finding_id: str = "fnd_test_1") -> Rebuttal:
    return Rebuttal(
        id="rbt_test_1",
        finding_id=finding_id,
        submitted_by="sys_ghost_factory_demo",
        submitted_at=_NOW,
        argument="The attached report covers commit abc123, not def456.",
        evidence_ids=["ev_ghost_002"],
        requested_outcome=RebuttalRequestedOutcome.WITHDRAWN,
    )


def _job(finding_id: str = "fnd_test_1") -> VerificationJob:
    return VerificationJob(
        id="vbj_test_1",
        finding_id=finding_id,
        check_id="tests.required_suite",
        requested_by="principal_operator",
        requested_at=_NOW,
        status=VerificationJobStatus.SUCCEEDED,
        outcome=VerificationOutcome.SUPPORTED,
        result_summary="Required suite passed on abc123.",
        independence="re-run of the same critic; no independent method exists",
        evidence_ids=["ev_ghost_002"],
        tool_run_refs=["tool_1"],
        started_at=_NOW,
        completed_at=_NOW,
    )


def _disposition(
    finding_id: str = "fnd_test_1",
    *,
    disposition_id: str = "dsp_test_1",
    supersedes: str | None = None,
    decided_at: datetime = _NOW,
) -> Disposition:
    return Disposition(
        id=disposition_id,
        finding_id=finding_id,
        outcome=DispositionOutcome.REJECTED,
        rationale="The premise that verification was missing does not hold.",
        decided_by="principal_operator",
        decided_at=decided_at,
        verification_run_ids=["vbj_test_1"],
        policy_version="1.0",
        supersedes_disposition_id=supersedes,
    )


def _policy_version(
    *, rule_id: str = "policy.approval_no_self_approval", version: str = "1.0"
) -> PolicyVersion:
    return PolicyVersion(
        id="plv_test_1",
        rule_id=rule_id,
        version=version,
        digest=_DIGEST,
        changed_by="principal_operator",
        changed_at=_NOW,
        change_reason="Initial registration of the approval policy.",
    )


def test_rebuttal_round_trips(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        stored = RebuttalRepository(session).add(_rebuttal())
        session.flush()

        loaded = RebuttalRepository(session).get(stored.id)
        assert loaded is not None
        assert loaded == stored
        assert loaded.requested_outcome is RebuttalRequestedOutcome.WITHDRAWN


def test_rebuttal_list_is_ordered_oldest_first(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = RebuttalRepository(session)
        first = _rebuttal()
        first.submitted_at = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
        repository.add(first)
        second = _rebuttal()
        second.id = "rbt_test_2"
        second.submitted_at = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
        repository.add(second)
        session.flush()

        listed = repository.list(finding_id="fnd_test_1")
        assert [item.id for item in listed] == ["rbt_test_1", "rbt_test_2"]


def test_verification_job_round_trips(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        stored = VerificationJobRepository(session).add(_job())
        session.flush()

        loaded = VerificationJobRepository(session).get(stored.id)
        assert loaded is not None
        assert loaded == stored
        assert loaded.outcome is VerificationOutcome.SUPPORTED


def test_queued_jobs_are_returned_separately_from_finished_ones(
    session_factory: object,
) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = VerificationJobRepository(session)
        queued = _job()
        queued.id = "vbj_queued"
        queued.status = VerificationJobStatus.QUEUED
        queued.outcome = None
        queued.result_summary = None
        queued.started_at = None
        queued.completed_at = None
        repository.add(queued)
        repository.add(_job())
        session.flush()

        assert [job.id for job in repository.list_queued()] == ["vbj_queued"]
        assert len(repository.list(finding_id="fnd_test_1")) == 2


def test_disposition_chain_reconstructs_in_order(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = DispositionRepository(session)
        repository.add(_disposition(disposition_id="dsp_1", supersedes=None))
        repository.add(
            _disposition(
                disposition_id="dsp_2", supersedes="dsp_1", decided_at=_NOW.replace(hour=13)
            )
        )
        repository.add(
            _disposition(
                disposition_id="dsp_3", supersedes="dsp_2", decided_at=_NOW.replace(hour=14)
            )
        )
        session.flush()

        chain = repository.chain("dsp_3")
        assert [item.id for item in chain] == ["dsp_1", "dsp_2", "dsp_3"]
        assert [item.supersedes_disposition_id for item in chain] == [None, "dsp_1", "dsp_2"]
        assert repository.latest("fnd_test_1") is not None
        assert repository.latest("fnd_test_1").id == "dsp_3"


def test_a_disposition_can_be_superseded_only_once(session_factory: object) -> None:
    """The chain must never branch: two dispositions cannot supersede the same predecessor."""

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = DispositionRepository(session)
        repository.add(_disposition(disposition_id="dsp_1", supersedes=None))
        repository.add(_disposition(disposition_id="dsp_2", supersedes="dsp_1"))
        session.flush()

        branching = _disposition(
            disposition_id="dsp_4", supersedes="dsp_1", decided_at=_NOW.replace(hour=16)
        )
        branching.rationale = "A second disposition trying to supersede dsp_1."

        with pytest.raises(IntegrityError):
            repository.add(branching)
            session.flush()
        # The failed flush left the transaction unusable; roll it back so the
        # session_scope block can exit cleanly.
        session.rollback()


def test_policy_version_round_trips_and_latest_is_newest(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = PolicyVersionRepository(session)
        repository.add(_policy_version(version="1.0"))
        newer = _policy_version(version="1.1")
        newer.id = "plv_test_2"
        newer.changed_at = _NOW.replace(hour=13)
        newer.change_reason = "Tighten the self-approval rule."
        repository.add(newer)
        session.flush()

        loaded = repository.get("plv_test_2")
        assert loaded is not None
        assert loaded == newer
        assert repository.latest("policy.approval_no_self_approval") is not None
        assert repository.latest("policy.approval_no_self_approval").version == "1.1"


def test_the_same_policy_version_cannot_be_recorded_twice(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = PolicyVersionRepository(session)
        repository.add(_policy_version(version="1.0"))
        session.flush()

        duplicate = _policy_version(version="1.0")
        duplicate.id = "plv_test_2"
        duplicate.change_reason = "A second reason for the same version."

        with pytest.raises(IntegrityError):
            repository.add(duplicate)
            session.flush()
        # The failed flush left the transaction unusable; roll it back so the
        # session_scope block can exit cleanly.
        session.rollback()


def test_disposition_count_reflects_the_chain_length(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = DispositionRepository(session)
        assert repository.count_for_finding("fnd_test_1") == 0
        repository.add(_disposition())
        session.flush()
        assert repository.count_for_finding("fnd_test_1") == 1
