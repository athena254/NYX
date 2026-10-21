"""Acceptance scenario 24: every confirmed finding passes the C-010 self-consistency critic.

The gate lives in ``nyx.findings.lifecycle`` (``_SELF_CONSISTENCY_GATED`` and the
``self_consistency_gate`` seam), not in the critic: C-010 still runs standalone as a
store-global check, and the lifecycle refuses to confirm a finding that would fail it.

These tests assert the *enforcement*. The control matters as much as the refusals -- a
gate that refused everything would pass every negative test here, so the first test
confirms a genuinely consistent finding, and the last proves the gate does not leak
into ordinary progress transitions.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete
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
)
from nyx.domain.evidence import EvidenceItem
from nyx.findings.lifecycle import (
    FindingLifecycle,
    InvalidLifecycleTransitionError,
    TransitionRequest,
)
from nyx.findings.models import CriticRun, Finding, Reasoning, RecommendedAction
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.critic_runs import CriticRunRepository
from nyx.storage.repositories.evidence import EvidenceRepository
from nyx.storage.repositories.findings import FindingRepository
from nyx.storage.tables import AuditRow, EvidenceRow

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_ACTOR = PrincipalRef(id="principal_operator", kind=PrincipalKind.HUMAN)


class _AllowAllChecker:
    """Test double for the Phase-4 capability seam: grants every capability."""

    def has_capability(self, principal: PrincipalRef, capability: object) -> bool:
        return True


def _add_evidence(session: Session, evidence_id: str) -> None:
    """Store the evidence row a finding cites, so the fixture is genuinely consistent."""

    repository = EvidenceRepository(session)
    if repository.get(evidence_id) is not None:
        return
    repository.add(
        EvidenceItem(
            id=evidence_id,
            kind=EvidenceKind.CI_TEST_RESULT,
            title="CI passed",
            description="All checks passed.",
            source_system_id="sys_s24",
            source_ref="https://ci.example.invalid/runs/1",
            scope=Scope(system_id="sys_s24", artifact="demo", commit="abc123"),
            captured_at=_NOW,
            valid_from=_NOW,
            integrity_method=IntegrityMethod.HASH,
            relevance_to_finding="fnd_s24",
            limitations=["Synthetic test evidence."],
            sensitivity=Sensitivity.INTERNAL,
            access_policy_ref="test",
            retention_class="standard",
        )
    )


def _persist_finding(
    session: Session,
    *,
    status: FindingStatus = FindingStatus.DETECTED,
    supporting: tuple[str, ...] = ("ev_s24",),
) -> Finding:
    """A consistent finding: resolvable evidence, a critic run with a rationale."""

    _add_evidence(session, "ev_s24")
    run = CriticRun(
        critic_id="critic_s24",
        critic_version="1.0.0",
        input_refs=["clm_s24"],
        started_at=_NOW,
        completed_at=_NOW,
        result=CriticResultStatus.FINDING,
        rationale="recorded rationale",
    )
    CriticRunRepository(session).add(run)
    finding = Finding(
        title="Scenario 24 finding",
        summary="A finding exercised by the scenario 24 gate tests.",
        category=FindingCategory.MISSING_VERIFICATION,
        severity=Severity.MEDIUM,
        scope=Scope(system_id="sys_s24", artifact="demo", commit="abc123"),
        affected_system_ids=["sys_s24"],
        claim_ids=["clm_s24"],
        evidence_ids=list(supporting),
        supporting_evidence_ids=list(supporting),
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


def _request(finding_id: str, target: FindingStatus, request_id: str) -> TransitionRequest:
    return TransitionRequest(
        finding_id=finding_id,
        target_status=target,
        actor=_ACTOR,
        request_id=request_id,
        rationale="recorded rationale",
    )


def _walk_to_verification_pending(session: Session, finding_id: str) -> None:
    """Drive a finding along the legal path to one edge before ``confirmed``."""

    lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())
    lifecycle.transition(_request(finding_id, FindingStatus.EVIDENCE_PENDING, "req_s24_a"))
    lifecycle.transition(_request(finding_id, FindingStatus.CHALLENGED, "req_s24_b"))
    lifecycle.transition(
        TransitionRequest(
            finding_id=finding_id,
            target_status=FindingStatus.VERIFICATION_PENDING,
            actor=_ACTOR,
            request_id="req_s24_c",
            rationale="recorded rationale",
            rebuttal_bypass_policy="objectively reproducible failed check",
        )
    )


# ---------------------------------------------------------------------------
# Control: a genuinely consistent finding clears the gate.
# ---------------------------------------------------------------------------


def test_control_a_consistent_finding_can_be_confirmed(session_factory: SessionFactory) -> None:
    with session_scope(session_factory) as session:
        finding = _persist_finding(session)
        _walk_to_verification_pending(session, finding.id)

        FindingLifecycle(session, capability_checker=_AllowAllChecker()).transition(
            _request(finding.id, FindingStatus.CONFIRMED, "req_s24_ok")
        )

        stored = FindingRepository(session).get(finding.id)
        assert stored is not None
        assert stored.status is FindingStatus.CONFIRMED


# ---------------------------------------------------------------------------
# Refusals: each one must leave the stored row exactly as it was.
# ---------------------------------------------------------------------------


def test_dangling_evidence_reference_blocks_confirmation(
    session_factory: SessionFactory,
) -> None:
    with session_scope(session_factory) as session:
        finding = _persist_finding(session)
        _walk_to_verification_pending(session, finding.id)
        # Break the store: the finding cites evidence nobody can inspect any more.
        session.execute(delete(EvidenceRow).where(EvidenceRow.id == "ev_s24"))
        session.flush()

        with pytest.raises(InvalidLifecycleTransitionError, match="self-consistency"):
            FindingLifecycle(session, capability_checker=_AllowAllChecker()).transition(
                _request(finding.id, FindingStatus.CONFIRMED, "req_s24_dangling")
            )

        stored = FindingRepository(session).get(finding.id)
        assert stored is not None
        assert stored.status is FindingStatus.VERIFICATION_PENDING


def test_broken_audit_chain_blocks_confirmation(session_factory: SessionFactory) -> None:
    from sqlalchemy import update

    with session_scope(session_factory) as session:
        finding = _persist_finding(session)
        _walk_to_verification_pending(session, finding.id)
        # Tamper with a stored audit entry: the chain no longer recomputes.
        session.execute(update(AuditRow).where(AuditRow.sequence == 1).values(reason="tampered"))
        session.flush()

        with pytest.raises(InvalidLifecycleTransitionError, match="self-consistency"):
            FindingLifecycle(session, capability_checker=_AllowAllChecker()).transition(
                _request(finding.id, FindingStatus.CONFIRMED, "req_s24_chain")
            )

        stored = FindingRepository(session).get(finding.id)
        assert stored is not None
        assert stored.status is FindingStatus.VERIFICATION_PENDING


def test_finding_without_recorded_transitions_blocks_confirmation(
    session_factory: SessionFactory,
) -> None:
    """The documented model derives a row's status from its latest valid transition."""

    with session_scope(session_factory) as session:
        finding = _persist_finding(session, status=FindingStatus.VERIFICATION_PENDING)

        with pytest.raises(InvalidLifecycleTransitionError, match="self-consistency"):
            FindingLifecycle(session, capability_checker=_AllowAllChecker()).transition(
                _request(finding.id, FindingStatus.CONFIRMED, "req_s24_history")
            )

        stored = FindingRepository(session).get(finding.id)
        assert stored is not None
        assert stored.status is FindingStatus.VERIFICATION_PENDING


def test_partially_confirmed_is_gated_too(session_factory: SessionFactory) -> None:
    """A partial confirmation is still a positive verdict, so it clears the same bar."""

    with session_scope(session_factory) as session:
        finding = _persist_finding(session)
        _walk_to_verification_pending(session, finding.id)
        session.execute(delete(EvidenceRow).where(EvidenceRow.id == "ev_s24"))
        session.flush()

        with pytest.raises(InvalidLifecycleTransitionError, match="self-consistency"):
            FindingLifecycle(session, capability_checker=_AllowAllChecker()).transition(
                _request(finding.id, FindingStatus.PARTIALLY_CONFIRMED, "req_s24_partial")
            )

        stored = FindingRepository(session).get(finding.id)
        assert stored is not None
        assert stored.status is FindingStatus.VERIFICATION_PENDING


# ---------------------------------------------------------------------------
# The seam: a caller may substitute which check runs, but not switch it off.
# ---------------------------------------------------------------------------


def test_the_gate_is_a_seam_but_has_no_off_switch(session_factory: SessionFactory) -> None:
    with session_scope(session_factory) as session:
        finding = _persist_finding(session)
        _walk_to_verification_pending(session, finding.id)

        def refusing(session: Session, finding_id: str) -> list[str]:
            return ["injected violation"]

        with pytest.raises(InvalidLifecycleTransitionError, match="injected violation"):
            FindingLifecycle(
                session,
                capability_checker=_AllowAllChecker(),
                self_consistency_gate=refusing,
            ).transition(_request(finding.id, FindingStatus.CONFIRMED, "req_s24_injected"))

        def allowing(session: Session, finding_id: str) -> list[str]:
            return []

        FindingLifecycle(
            session, capability_checker=_AllowAllChecker(), self_consistency_gate=allowing
        ).transition(_request(finding.id, FindingStatus.CONFIRMED, "req_s24_allowed"))

        stored = FindingRepository(session).get(finding.id)
        assert stored is not None
        assert stored.status is FindingStatus.CONFIRMED


def test_the_gate_does_not_block_ordinary_progress(session_factory: SessionFactory) -> None:
    """Scoping: an inconsistent store must not freeze the whole lifecycle."""

    with session_scope(session_factory) as session:
        finding = _persist_finding(session, supporting=("ev_never_stored",))
        FindingLifecycle(session, capability_checker=_AllowAllChecker()).transition(
            _request(finding.id, FindingStatus.EVIDENCE_PENDING, "req_s24_progress")
        )

        stored = FindingRepository(session).get(finding.id)
        assert stored is not None
        assert stored.status is FindingStatus.EVIDENCE_PENDING
