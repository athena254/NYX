"""Unit tests for the finding lifecycle state machine (SPEC §6).

Covers acceptance scenario 12: an invalid lifecycle transition is rejected AND
the stored state is unchanged -- asserted on the re-read record, not just on the
error. Also one test per SPEC §6 rule (confirmed needs rationale + evidence,
rebuttal bypass must be recorded, inconclusive is not silently converted,
fixed_pending_verification is not closed), the capability seam, the audit
trail, and a Hypothesis property test that fuzzes arbitrary transition
sequences: no sequence can drive a record into an invalid state, and a refused
transition never mutates the record.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from nyx.config import Settings
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
from nyx.error_codes import ErrorCode
from nyx.findings.lifecycle import (
    CapabilityDeniedError,
    FindingLifecycle,
    InvalidLifecycleTransitionError,
    TransitionRequest,
    allowed_transitions,
)
from nyx.findings.models import CriticRun, Finding, Reasoning, RecommendedAction
from nyx.storage.db import (
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from nyx.storage.repositories.audit import AuditRepository
from nyx.storage.repositories.critic_runs import CriticRunRepository
from nyx.storage.repositories.evidence import EvidenceRepository
from nyx.storage.repositories.findings import FindingRepository

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_ACTOR = PrincipalRef(id="principal_operator", kind=PrincipalKind.HUMAN)
_REQUEST_IDS = count(1)


class _AllowAllChecker:
    """Test double for the Phase-4 seam: grants every capability."""

    def has_capability(self, principal: PrincipalRef, capability: object) -> bool:
        return True


def _add_evidence(session: Session, evidence_id: str) -> EvidenceItem:
    """Store the evidence row a finding references.

    C-010 resolves a finding's evidence ids against the store, so a finding citing
    an id nobody stored is a self-consistency violation. Since acceptance scenario
    24 gates ``confirmed`` on C-010, these fixtures must be genuinely consistent
    rather than merely plausible.
    """

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
            source_system_id="sys_lifecycle_test",
            source_ref="https://ci.example.invalid/runs/1",
            scope=Scope(system_id="sys_lifecycle_test", artifact="demo", commit="abc123"),
            captured_at=_NOW,
            valid_from=_NOW,
            integrity_method=IntegrityMethod.HASH,
            relevance_to_finding="fnd_lifecycle_test",
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
    supporting: tuple[str, ...] = ("ev_test",),
) -> Finding:
    """Persist a finding with its critic run so it round-trips through storage."""

    _add_evidence(session, "ev_test")
    run = CriticRun(
        critic_id="critic_lifecycle_test",
        critic_version="1.0",
        input_refs=["clm_lifecycle_test"],
        started_at=_NOW,
        completed_at=_NOW,
        result=CriticResultStatus.FINDING,
        rationale="test critic run",
    )
    CriticRunRepository(session).add(run)
    finding = Finding(
        title="Lifecycle test finding",
        summary="A finding exercised by lifecycle tests.",
        category=FindingCategory.MISSING_VERIFICATION,
        severity=Severity.MEDIUM,
        scope=Scope(system_id="sys_lifecycle_test", artifact="demo", commit="abc123"),
        affected_system_ids=["sys_lifecycle_test"],
        claim_ids=["clm_lifecycle_test"],
        evidence_ids=list(supporting),
        supporting_evidence_ids=list(supporting),
        reasoning=Reasoning(observations=["Something was observed."]),
        recommended_action=RecommendedAction(text="Investigate."),
        critic_runs=[run],
        status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )
    # Pin created_at as well so the round-trip keeps updated_at >= created_at.
    FindingRepository(session).add(finding)
    CriticRunRepository(session).link_finding(run.id, finding.id)
    return finding


def _request(
    finding_id: str,
    target: FindingStatus,
    *,
    rationale: str | None = "recorded rationale",
    **extra: object,
) -> TransitionRequest:
    return TransitionRequest(
        finding_id=finding_id,
        target_status=target,
        actor=_ACTOR,
        request_id=f"req_{next(_REQUEST_IDS)}",
        rationale=rationale,
        **extra,
    )


# ---------------------------------------------------------------------------
# Scenario 12: an invalid transition is rejected and the stored state is
# unchanged (asserted on the record, not just the error).
# ---------------------------------------------------------------------------


def test_scenario_12_invalid_transition_rejected_and_state_unchanged(
    session_factory: object,
) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session)
        lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())

        with pytest.raises(InvalidLifecycleTransitionError) as excinfo:
            # detected -> confirmed is not an edge in the SPEC §6 graph.
            lifecycle.transition(_request(finding.id, FindingStatus.CONFIRMED))

        assert excinfo.value.code is ErrorCode.INVALID_LIFECYCLE_TRANSITION

        reloaded = FindingRepository(session).get(finding.id)
        assert reloaded is not None
        assert reloaded.status is FindingStatus.DETECTED
        assert reloaded.updated_at == _NOW
        # A refused transition writes nothing to the audit log either.
        assert AuditRepository(session).list(target_type="finding", target_id=finding.id) == []


# ---------------------------------------------------------------------------
# Property: no arbitrary transition sequence can reach an invalid state, and a
# refused transition never mutates the record.
# ---------------------------------------------------------------------------


# The 200ms default wall-clock deadline is wrong for a property test that copies a
# migrated SQLite database and drives up to ten transactions per example: the
# bottleneck is file I/O, not the state machine. This follows the same convention
# as tests/unit/test_ingestion_properties.py.
@hypothesis_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(targets=st.lists(st.sampled_from(list(FindingStatus)), max_size=10))
def test_no_transition_sequence_reaches_an_invalid_state(
    migration_template: Path, targets: list[FindingStatus]
) -> None:
    # Each hypothesis input gets its own database copy: function-scoped fixtures
    # are not reset between inputs, so the per-input state must be built here.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "nyx.sqlite3"
        shutil.copy(migration_template, database)
        engine = create_engine_from_settings(
            Settings(
                database_url=f"sqlite:///{database.as_posix()}",
                data_dir=root,
            )
        )
        try:
            session_factory = create_session_factory(engine)
            with session_scope(session_factory) as session:
                finding = _persist_finding(session)
                lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())

                current = FindingStatus.DETECTED
                accepted = 0
                for target in targets:
                    # Supply every optional extra so only the graph (and its
                    # edge rules) can refuse a move, never a missing
                    # rationale/evidence/waiver.
                    request = TransitionRequest(
                        finding_id=finding.id,
                        target_status=target,
                        actor=_ACTOR,
                        request_id=f"req_{next(_REQUEST_IDS)}",
                        rationale="fuzz rationale",
                        verification_result=VerificationResult.VERIFIED,
                        rebuttal_bypass_policy="policy.reproducible_failure",
                        duplicate_of="fnd_other",
                    )
                    legal = target in allowed_transitions(current)
                    try:
                        lifecycle.transition(request)
                    except InvalidLifecycleTransitionError:
                        assert not legal, "a legal transition was refused"
                        reloaded = FindingRepository(session).get(finding.id)
                        assert reloaded is not None
                        assert reloaded.status is current, "a refused transition mutated the record"
                        continue
                    assert legal, "an illegal transition was applied"
                    accepted += 1
                    current = target
                    reloaded = FindingRepository(session).get(finding.id)
                    assert reloaded is not None
                    assert reloaded.status is target

                # Every accepted transition produced exactly one audit entry.
                entries = AuditRepository(session).list(target_type="finding", target_id=finding.id)
                assert len(entries) == accepted
                final = FindingRepository(session).get(finding.id)
                assert final is not None
                assert final.status is current
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# SPEC §6 rule: confirmed requires rationale + relevant evidence (invariant 1).
# ---------------------------------------------------------------------------


def test_confirmed_without_evidence_refused(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        # Start one edge away from confirmed so the graph admits the move and
        # the evidence rule (not the graph) is what refuses it.
        finding = _persist_finding(
            session, status=FindingStatus.VERIFICATION_PENDING, supporting=()
        )
        lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())

        with pytest.raises(InvalidLifecycleTransitionError, match="evidence"):
            lifecycle.transition(_request(finding.id, FindingStatus.CONFIRMED))

        reloaded = FindingRepository(session).get(finding.id)
        assert reloaded is not None
        assert reloaded.status is FindingStatus.VERIFICATION_PENDING


def test_confirmed_without_rationale_refused(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session, status=FindingStatus.VERIFICATION_PENDING)
        lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())

        with pytest.raises(InvalidLifecycleTransitionError, match="rationale"):
            lifecycle.transition(_request(finding.id, FindingStatus.CONFIRMED, rationale=None))

        reloaded = FindingRepository(session).get(finding.id)
        assert reloaded is not None
        assert reloaded.status is FindingStatus.VERIFICATION_PENDING


# ---------------------------------------------------------------------------
# SPEC §6 rule: fixed_pending_verification is not closed; closure needs a
# verification result or an explicitly authorised waiver.
# ---------------------------------------------------------------------------


def test_fixed_pending_verification_is_not_closed(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session, status=FindingStatus.FIXED_PENDING_VERIFICATION)
        lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())

        with pytest.raises(InvalidLifecycleTransitionError, match="verification result"):
            lifecycle.transition(_request(finding.id, FindingStatus.CLOSED))

        reloaded = FindingRepository(session).get(finding.id)
        assert reloaded is not None
        assert reloaded.status is FindingStatus.FIXED_PENDING_VERIFICATION
        assert reloaded.status is not FindingStatus.CLOSED

        # With a recorded verification result, closure is allowed.
        lifecycle.transition(
            _request(
                finding.id,
                FindingStatus.CLOSED,
                verification_result=VerificationResult.VERIFIED,
            )
        )
        reloaded = FindingRepository(session).get(finding.id)
        assert reloaded is not None
        assert reloaded.status is FindingStatus.CLOSED


# ---------------------------------------------------------------------------
# SPEC §6 rule: skipping rebuttal is allowed only under a documented policy,
# and the bypass must be recorded, not silent.
# ---------------------------------------------------------------------------


def test_rebuttal_bypass_must_be_recorded(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session, status=FindingStatus.CHALLENGED)
        lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())

        with pytest.raises(InvalidLifecycleTransitionError, match="documented policy"):
            lifecycle.transition(_request(finding.id, FindingStatus.VERIFICATION_PENDING))

        reloaded = FindingRepository(session).get(finding.id)
        assert reloaded is not None
        assert reloaded.status is FindingStatus.CHALLENGED

        lifecycle.transition(
            _request(
                finding.id,
                FindingStatus.VERIFICATION_PENDING,
                rebuttal_bypass_policy="policy.reproducible_failure",
            )
        )
        entries = AuditRepository(session).list(target_type="finding", target_id=finding.id)
        assert len(entries) == 1
        assert "policy.reproducible_failure" in entries[0].reason


# ---------------------------------------------------------------------------
# SPEC §6 rule: inconclusive must never be silently converted into a positive
# or negative verdict.
# ---------------------------------------------------------------------------


def test_inconclusive_is_not_silently_converted(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session)
        lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())

        for target in (
            FindingStatus.EVIDENCE_PENDING,
            FindingStatus.CHALLENGED,
            FindingStatus.REBUTTAL_PENDING,
            FindingStatus.VERIFICATION_PENDING,
            FindingStatus.INCONCLUSIVE,
        ):
            lifecycle.transition(_request(finding.id, target))

        for verdict in (
            FindingStatus.CONFIRMED,
            FindingStatus.PARTIALLY_CONFIRMED,
            FindingStatus.REJECTED,
        ):
            with pytest.raises(InvalidLifecycleTransitionError):
                lifecycle.transition(_request(finding.id, verdict))

        reloaded = FindingRepository(session).get(finding.id)
        assert reloaded is not None
        assert reloaded.status is FindingStatus.INCONCLUSIVE


# ---------------------------------------------------------------------------
# SPEC §6 rule: only principals with the relevant capability may change a
# disposition or accept risk. The default checker denies everything.
# ---------------------------------------------------------------------------


def test_capability_seam_refuses_principal_without_capability(
    session_factory: object,
) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session)
        lifecycle = FindingLifecycle(session)  # default: deny-all checker

        with pytest.raises(CapabilityDeniedError) as excinfo:
            lifecycle.transition(_request(finding.id, FindingStatus.CONFIRMED))

        assert excinfo.value.code is ErrorCode.CAPABILITY_REQUIRED
        reloaded = FindingRepository(session).get(finding.id)
        assert reloaded is not None
        assert reloaded.status is FindingStatus.DETECTED

        # Ordinary progress is not a disposition change and needs no capability.
        lifecycle.transition(_request(finding.id, FindingStatus.EVIDENCE_PENDING))
        reloaded = FindingRepository(session).get(finding.id)
        assert reloaded is not None
        assert reloaded.status is FindingStatus.EVIDENCE_PENDING


# ---------------------------------------------------------------------------
# Every accepted transition writes an audit entry via nyx.audit.writer.
# ---------------------------------------------------------------------------


def test_accepted_transition_writes_audit_entry(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session)
        lifecycle = FindingLifecycle(session)

        lifecycle.transition(_request(finding.id, FindingStatus.EVIDENCE_PENDING))

        entries = AuditRepository(session).list(target_type="finding", target_id=finding.id)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.action == "finding.transition"
        assert entry.target.type == "finding"
        assert entry.target.id == finding.id
        assert entry.actor == _ACTOR
        assert entry.outcome.value == "success"
        assert "detected" in entry.reason
        assert "evidence_pending" in entry.reason


# ---------------------------------------------------------------------------
# The full SPEC §6 happy path, end to end.
# ---------------------------------------------------------------------------


def test_full_happy_path_to_closed(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = _persist_finding(session)
        lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())

        path = (
            FindingStatus.EVIDENCE_PENDING,
            FindingStatus.CHALLENGED,
            FindingStatus.REBUTTAL_PENDING,
            FindingStatus.VERIFICATION_PENDING,
            FindingStatus.CONFIRMED,
            FindingStatus.REMEDIATION_PENDING,
            FindingStatus.FIXED_PENDING_VERIFICATION,
        )
        for target in path:
            lifecycle.transition(_request(finding.id, target))
        lifecycle.transition(
            _request(
                finding.id,
                FindingStatus.CLOSED,
                verification_result=VerificationResult.VERIFIED,
            )
        )

        reloaded = FindingRepository(session).get(finding.id)
        assert reloaded is not None
        assert reloaded.status is FindingStatus.CLOSED
        entries = AuditRepository(session).list(target_type="finding", target_id=finding.id)
        assert len(entries) == len(path) + 1
