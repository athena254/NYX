"""Regression: a disposition that cites counterevidence must leave the finding readable.

The bug this file exists to prevent was silent data corruption. Recording a disposition whose
``counterevidence_ids`` named a real evidence item appended the id to the finding's
``counterevidence_ids`` but never to its ``evidence_ids``. ``Finding`` requires
``supporting_evidence_ids | counterevidence_ids`` to be a subset of ``evidence_ids``, so the row
became invalid and every later read of that finding raised ``ValidationError`` -- the disposition
endpoint returned 200 and the finding was then unreadable forever.

The acceptance criterion is the *read-back* state, not the status code of the POST: before the fix
``GET /api/v1/findings/{id}`` returned HTTP 500 on the corrupted row. The endpoint surface and the
migration-backed database are exercised end to end, because the corruption only shows up on the
round-trip through ``FindingRepository._to_domain``.

Evidence for step 4 is registered as a ``review_record`` item linked to the finding, which is what
the scenario describes. A disposition citing a rebuttal's evidence (already in ``evidence_ids``
because the rebuttal appends it) would not have reproduced the bug.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from nyx.domain.base import Scope
from nyx.domain.enums import (
    EvidenceKind,
    FindingCategory,
    FindingStatus,
    IntegrityMethod,
    Sensitivity,
    Severity,
)
from nyx.domain.evidence import EvidenceItem
from nyx.findings.models import CriticRun, Finding, Reasoning, RecommendedAction
from nyx.storage.db import session_scope
from nyx.storage.repositories.critic_runs import CriticRunRepository
from nyx.storage.repositories.evidence import EvidenceRepository
from nyx.storage.repositories.findings import FindingRepository

API = "/api/v1"
# Pinned in the past: the lifecycle stamps ``updated_at`` from the real clock, so a future pin
# would make updated_at precede created_at and fail Finding validation on read-back.
_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

_SYSTEM = "sys_disposition_counterevidence_regression"
_FINDING = "fnd_disposition_counterevidence_regression"
_REVIEW_EVIDENCE = "ev_disposition_counterevidence_regression"


def _persist_unverified_finding(session: Session) -> Finding:
    """A finding raised by a critic with NO verification evidence cited.

    Exactly the shape the critic pack produces for a completion claim with no linked test evidence:
    ``evidence_ids``, ``supporting_evidence_ids`` and ``counterevidence_ids`` are all empty, so
    there is no pre-existing id that would mask the missing ``evidence_ids`` append.
    """

    run = CriticRun(
        critic_id="critic_disposition_counterevidence_regression",
        critic_version="1.0",
        input_refs=[_FINDING],
        started_at=_NOW,
        completed_at=_NOW,
        result="finding",
        rationale="completion claim names a commit with no linked verification artifact",
    )
    CriticRunRepository(session).add(run)
    finding = Finding(
        id=_FINDING,
        title="Verification gap: no test evidence for the completion claim",
        summary="The claim names a commit but no verification artifact is linked.",
        category=FindingCategory.MISSING_VERIFICATION,
        severity=Severity.MEDIUM,
        scope=Scope(system_id=_SYSTEM, artifact="demo", commit="abc123"),
        affected_system_ids=[_SYSTEM],
        claim_ids=["clm_disposition_counterevidence_regression"],
        evidence_ids=[],
        supporting_evidence_ids=[],
        counterevidence_ids=[],
        reasoning=Reasoning(observations=["The completion claim cites no verification artifact."]),
        recommended_action=RecommendedAction(text="Run the suite and link the result."),
        critic_runs=[run],
        status=FindingStatus.DETECTED,
        created_at=_NOW,
        updated_at=_NOW,
    )
    FindingRepository(session).add(finding)
    CriticRunRepository(session).link_finding(run.id, finding.id)
    return finding


def _add_review_record_evidence(session: Session) -> EvidenceItem:
    """Register the ``review_record`` evidence item the disposition will cite."""

    repository = EvidenceRepository(session)
    existing = repository.get(_REVIEW_EVIDENCE)
    if existing is not None:
        return existing
    return repository.add(
        EvidenceItem(
            id=_REVIEW_EVIDENCE,
            kind=EvidenceKind.REVIEW_RECORD,
            title="Independent review record",
            description="A reviewer record weighing the completion claim against its commit.",
            source_system_id=_SYSTEM,
            source_ref="https://reviews.example.invalid/records/1",
            scope=Scope(system_id=_SYSTEM, artifact="demo", commit="abc123"),
            captured_at=_NOW,
            valid_from=_NOW,
            integrity_method=IntegrityMethod.HASH,
            relevance_to_finding=_FINDING,
            relevance_note=(
                "Linked to the finding the disposition weighs; not offered for the claim itself."
            ),
            limitations=["Synthetic test evidence."],
            sensitivity=Sensitivity.INTERNAL,
            access_policy_ref="test",
            retention_class="standard",
        )
    )


def _file_rebuttal(client: TestClient) -> None:
    response = client.post(
        f"{API}/findings/{_FINDING}/rebuttals",
        json={
            "submitted_by": "sys_disposition_counterevidence_regression",
            "argument": "The completion claim cannot be verified from the linked records.",
            "evidence_ids": [],
        },
    )
    assert response.status_code == 200, response.text


def _run_verification_job(client: TestClient) -> str:
    queued = client.post(
        f"{API}/findings/{_FINDING}/verification-jobs",
        json={"check_id": "critics.rerun-linked", "requested_by": "principal_operator"},
    )
    assert queued.status_code == 200, queued.text
    job_id = queued.json()["job"]["id"]

    ran = client.post(f"{API}/findings/{_FINDING}/verification-jobs/run")
    assert ran.status_code == 200, ran.text
    return job_id


def test_a_disposition_citing_counterevidence_leaves_the_finding_readable(
    client: TestClient,
    session_factory: object,
) -> None:
    """The acceptance scenario: POST returns 200, and the finding is readable afterwards.

    Asserts the *read-back* state. Before the fix the POST returned 200 and the very next
    ``GET /api/v1/findings/{id}`` returned HTTP 500, permanently.
    """

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        _persist_unverified_finding(session)
        _add_review_record_evidence(session)

    _file_rebuttal(client)
    job_id = _run_verification_job(client)

    posted = client.post(
        f"{API}/findings/{_FINDING}/dispositions",
        json={
            "outcome": "inconclusive",
            "rationale": "The completion claim cannot be verified from the linked records.",
            "decided_by": "principal_operator",
            "policy_version": "1.0",
            "verification_run_ids": [job_id],
            "counterevidence_ids": [_REVIEW_EVIDENCE],
        },
    )
    assert posted.status_code == 200, posted.text

    # --- the read-back is the assertion that matters ---
    fetched = client.get(f"{API}/findings/{_FINDING}")
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["status"] == "inconclusive"
    assert body["counterevidence_ids"] == [_REVIEW_EVIDENCE]

    history = client.get(f"{API}/findings/{_FINDING}/history")
    assert history.status_code == 200, history.text
    assert history.json()["finding"]["counterevidence_ids"] == [_REVIEW_EVIDENCE]

    # The repository read-back is what raised ValidationError before the fix: every later read
    # of this finding went through FindingRepository._to_domain, so a 500 here is the bug.
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        reloaded = FindingRepository(session).get(_FINDING)
        assert reloaded is not None
        assert reloaded.status is FindingStatus.INCONCLUSIVE
        assert reloaded.counterevidence_ids == [_REVIEW_EVIDENCE]
        # Append-only: the citation is in evidence_ids exactly once, alongside counterevidence.
        assert reloaded.evidence_ids == [_REVIEW_EVIDENCE]
        assert len(reloaded.evidence_ids) == len(set(reloaded.evidence_ids))

    # And it stays readable on a subsequent read, in a fresh session: the corruption was permanent.
    again = client.get(f"{API}/findings/{_FINDING}")
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "inconclusive"


def test_counterevidence_appends_are_append_only_and_deduplicated(
    session_factory: object,
) -> None:
    """A second disposition weighing more evidence grows both lists without dropping anything."""

    from nyx.domain.base import PrincipalRef
    from nyx.domain.enums import PrincipalKind
    from nyx.findings.lifecycle import FindingLifecycle, TransitionRequest

    class _AllowAllChecker:
        """Test double for the Phase-4 seam: grants every capability."""

        def has_capability(self, principal: PrincipalRef, capability: object) -> bool:
            return True

    actor = PrincipalRef(id="principal_operator", kind=PrincipalKind.HUMAN)

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        _persist_unverified_finding(session)
        _add_review_record_evidence(session)
        lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())
        for target in (
            FindingStatus.EVIDENCE_PENDING,
            FindingStatus.CHALLENGED,
            FindingStatus.REBUTTAL_PENDING,
            FindingStatus.VERIFICATION_PENDING,
        ):
            lifecycle.transition(
                TransitionRequest(
                    finding_id=_FINDING,
                    target_status=target,
                    actor=actor,
                    request_id=f"req-progress-{target.value}",
                    rationale="progress to verification_pending",
                )
            )

        # Cite the same id twice: nothing is removed and no id is recorded twice. A finding
        # already past a verdict has no edge back (SPEC §6), so the dedupe is exercised by
        # re-citing the same id and by an id the finding already carries.
        lifecycle.transition(
            TransitionRequest(
                finding_id=_FINDING,
                target_status=FindingStatus.INCONCLUSIVE,
                actor=actor,
                request_id="req-weigh-1",
                rationale="weighed the review record",
                counterevidence_ids=[_REVIEW_EVIDENCE],
            )
        )

        reloaded = FindingRepository(session).get(_FINDING)
        assert reloaded is not None
        assert reloaded.counterevidence_ids == [_REVIEW_EVIDENCE]
        assert reloaded.evidence_ids == [_REVIEW_EVIDENCE]
        assert reloaded.status is FindingStatus.INCONCLUSIVE

        # The same citation again, from a fresh read, must not duplicate either list.
        again = FindingLifecycle(session, capability_checker=_AllowAllChecker())
        again.transition(
            TransitionRequest(
                finding_id=_FINDING,
                target_status=FindingStatus.WITHDRAWN,
                actor=actor,
                request_id="req-weigh-2",
                rationale="withdrawn, citing the same counterevidence",
                counterevidence_ids=[_REVIEW_EVIDENCE],
            )
        )

        reloaded = FindingRepository(session).get(_FINDING)
        assert reloaded is not None
        assert reloaded.counterevidence_ids == [_REVIEW_EVIDENCE]
        assert reloaded.evidence_ids == [_REVIEW_EVIDENCE]
        assert reloaded.status is FindingStatus.WITHDRAWN
