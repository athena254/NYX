"""Acceptance scenario 23: a fixed finding stays open until verification or a waiver.

The rule is implemented in ``nyx.findings.lifecycle`` and unit-tested in
``tests/unit/test_finding_lifecycle.py::test_fixed_pending_verification_is_not_closed``. The
acceptance table for scenario 23 names this file as its owner, and until now that file did not exist
-- so the box was honestly unchecked even though the behaviour worked.

These tests are the integration counterpart: they run the rule against a real migrated database and
assert the *stored* state, not just the raised error. The distinction that matters is that a refused
closure must leave the row exactly as it was, because a finding that silently became closed would be
indistinguishable from one that was legitimately verified.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from nyx.critics.base import CriticResultStatus
from nyx.domain.base import PrincipalRef, Scope
from nyx.domain.enums import (
    EvidenceKind,
    FindingCategory,
    FindingStatus,
    IntegrityMethod,
    PrincipalKind,
    Sensitivity,
    Severity,
    VerificationResult,
)
from nyx.domain.evidence import EvidenceItem
from nyx.findings.lifecycle import (
    FindingLifecycle,
    InvalidLifecycleTransitionError,
    TransitionRequest,
)
from nyx.findings.models import CriticRun, Finding, Reasoning, RecommendedAction
from nyx.storage.db import session_scope
from nyx.storage.repositories.audit import AuditRepository
from nyx.storage.repositories.critic_runs import CriticRunRepository
from nyx.storage.repositories.evidence import EvidenceRepository
from nyx.storage.repositories.findings import FindingRepository

# Pinned in the past: the lifecycle stamps updated_at from the real clock, so a future
# pin would make updated_at precede created_at and fail Finding validation on read-back.
_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_ACTOR = PrincipalRef(id="principal_operator", kind=PrincipalKind.HUMAN)


class _AllowAllChecker:
    """Test double for the Phase-4 seam: grants every capability."""

    def has_capability(self, principal: PrincipalRef, capability: object) -> bool:
        return True


def _add_evidence(session: Session, evidence_id: str) -> EvidenceItem:
    """Store the evidence row a finding references (see the unit-test helper)."""

    repository = EvidenceRepository(session)
    existing = repository.get(evidence_id)
    if existing is not None:
        return existing
    return repository.add(
        EvidenceItem(
            id=evidence_id,
            kind=EvidenceKind.CI_TEST_RESULT,
            title="CI passed",
            description="All checks passed.",
            source_system_id="sys_scenario_23",
            source_ref="https://ci.example.invalid/runs/1",
            scope=Scope(system_id="sys_scenario_23", artifact="demo", commit="abc123"),
            captured_at=_NOW,
            valid_from=_NOW,
            integrity_method=IntegrityMethod.HASH,
            relevance_to_finding="fnd_scenario_23",
            limitations=["Synthetic test evidence."],
            sensitivity=Sensitivity.INTERNAL,
            access_policy_ref="test",
            retention_class="standard",
        )
    )


def _persist_finding(session: Session, *, status: FindingStatus) -> Finding:
    """Persist a finding with its critic run, in the given state."""

    _add_evidence(session, "ev_scenario_23")
    run = CriticRun(
        critic_id="critic_scenario_23",
        critic_version="1.0",
        input_refs=["clm_scenario_23"],
        started_at=_NOW,
        completed_at=_NOW,
        result=CriticResultStatus.FINDING,
        rationale="test critic run",
    )
    CriticRunRepository(session).add(run)
    finding = Finding(
        id="fnd_scenario_23",
        title="Scenario 23 finding",
        summary="A finding exercised by the scenario 23 integration test.",
        category=FindingCategory.MISSING_VERIFICATION,
        severity=Severity.MEDIUM,
        scope=Scope(system_id="sys_scenario_23", artifact="demo", commit="abc123"),
        affected_system_ids=["sys_scenario_23"],
        claim_ids=["clm_scenario_23"],
        evidence_ids=["ev_scenario_23"],
        supporting_evidence_ids=["ev_scenario_23"],
        reasoning=Reasoning(observations=["Something was observed."]),
        recommended_action=RecommendedAction(text="Investigate."),
        critic_runs=[run],
        status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )
    FindingRepository(session).add(finding)
    CriticRunRepository(session).link_finding(run.id, finding.id)
    return finding


def _transition(
    lifecycle: FindingLifecycle,
    finding_id: str,
    target: FindingStatus,
    **extra: object,
) -> None:
    lifecycle.transition(
        TransitionRequest(
            finding_id=finding_id,
            target_status=target,
            actor=_ACTOR,
            request_id=f"req-23-{target.value}",
            rationale="scenario 23 test",
            **extra,
        )
    )


def _walk_to_fixed_pending_verification(session: Session, finding_id: str) -> None:
    """Drive a freshly raised finding along the whole canonical path to remediation."""

    lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())
    for target in (
        FindingStatus.EVIDENCE_PENDING,
        FindingStatus.CHALLENGED,
        FindingStatus.REBUTTAL_PENDING,
        FindingStatus.VERIFICATION_PENDING,
        FindingStatus.CONFIRMED,
        FindingStatus.REMEDIATION_PENDING,
        FindingStatus.FIXED_PENDING_VERIFICATION,
    ):
        _transition(lifecycle, finding_id, target)


def test_a_fixed_finding_is_not_closed(session_factory: object) -> None:
    """The state name says what it means: fixed pending verification is not closed."""

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session, status=FindingStatus.DETECTED)
        _walk_to_fixed_pending_verification(session, finding.id)

        stored = FindingRepository(session).get(finding.id)
        assert stored is not None
        assert stored.status is FindingStatus.FIXED_PENDING_VERIFICATION
        assert stored.status is not FindingStatus.CLOSED


def test_closure_without_verification_or_waiver_is_refused_and_state_is_unchanged(
    session_factory: object,
) -> None:
    """The core of scenario 23: a fixed finding stays open without a verifier or a waiver."""

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session, status=FindingStatus.DETECTED)
        _walk_to_fixed_pending_verification(session, finding.id)

        lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())
        before = FindingRepository(session).get(finding.id)
        assert before is not None
        updated_at_before = before.updated_at

        with pytest.raises(InvalidLifecycleTransitionError, match="verification result"):
            _transition(lifecycle, finding.id, FindingStatus.CLOSED)

        # The refusal must not have touched the stored record.
        after = FindingRepository(session).get(finding.id)
        assert after is not None
        assert after.status is FindingStatus.FIXED_PENDING_VERIFICATION
        assert after.updated_at == updated_at_before
        # And no audit entry was written for a transition that did not happen.
        assert (
            AuditRepository(session).list(target_type="finding", target_id=finding.id)[-1].action
            == "finding.transition"
        )
        assert len(AuditRepository(session).list(target_type="finding", target_id=finding.id)) == 7


def test_a_recorded_verification_result_allows_closure(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session, status=FindingStatus.DETECTED)
        _walk_to_fixed_pending_verification(session, finding.id)

        lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())
        _transition(
            lifecycle,
            finding.id,
            FindingStatus.CLOSED,
            verification_result=VerificationResult.VERIFIED,
        )

        stored = FindingRepository(session).get(finding.id)
        assert stored is not None
        assert stored.status is FindingStatus.CLOSED


def test_a_documented_waiver_allows_closure(session_factory: object) -> None:
    """Closure is also allowed on an explicitly authorised waiver, and the waiver is recorded."""

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session, status=FindingStatus.DETECTED)
        _walk_to_fixed_pending_verification(session, finding.id)

        lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())
        _transition(
            lifecycle,
            finding.id,
            FindingStatus.CLOSED,
            waiver={"authorised_by": "principal_risk_owner", "reason": "Accepted as low impact."},
        )

        stored = FindingRepository(session).get(finding.id)
        assert stored is not None
        assert stored.status is FindingStatus.CLOSED

        entries = AuditRepository(session).list(target_type="finding", target_id=finding.id)
        assert "principal_risk_owner" in entries[-1].reason
