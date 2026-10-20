"""Unit tests for C-010 (NyxSelfConsistencyCritic).

Covers acceptance scenario 24: every confirmed finding passes the NYX self-consistency
critic -- and, critically, a store with a self-consistency violation does NOT pass it:

  -- A fully consistent store (confirmed finding reached through recorded, legal
     lifecycle transitions, valid evidence references, recorded critic versions, and a
     complete audit chain) stays quiet.
  -- A confirmed finding stored with no recorded lifecycle transitions fires.
  -- A tampered audit entry (edited after it was written) fires.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from nyx.critics.c010_nyx_self_consistency import (
    CRITIC_ID,
    CRITIC_VERSION,
    NyxSelfConsistencyCritic,
)
from nyx.critics.context import build_context
from nyx.domain.base import PrincipalRef, Scope
from nyx.domain.enums import (
    CriticResultStatus,
    EvidenceKind,
    FindingCategory,
    FindingStatus,
    IntegrityMethod,
    PrincipalKind,
    Sensitivity,
    Severity,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem
from nyx.findings.composer import compose_finding
from nyx.findings.lifecycle import FindingLifecycle, TransitionRequest
from nyx.findings.models import CriticRun, Finding, Reasoning, RecommendedAction
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.audit import AuditRepository
from nyx.storage.repositories.critic_runs import CriticRunRepository
from nyx.storage.repositories.evidence import EvidenceRepository
from nyx.storage.repositories.findings import FindingRepository
from nyx.storage.tables import AuditRow

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_ACTOR = PrincipalRef(id="principal_operator", kind=PrincipalKind.HUMAN)


class _AllowAllChecker:
    """Test double for the Phase-4 seam: grants every capability."""

    def has_capability(self, principal: PrincipalRef, capability: object) -> bool:
        return True


def _event() -> Event:
    return Event(
        source_system_id="sys_c010_test",
        event_type="agent.self_audit",
        occurred_at=_NOW,
        correlation_id="c010-corr-1",
        payload={},
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )


def _context() -> object:
    return build_context(target_event=_event(), now=_NOW)


def _scoped_context() -> object:
    """A context with a narrowed scope, as the registry always builds from a real claim/event.

    ``_context``'s bare system scope is fine for asserting the critic's own result, but the
    composer rejects a finding whose scope does not narrow beyond the system id -- which is what
    the registry never hands it.
    """

    event = _event()
    event.payload = {"commit": "abc123", "artifact": "demo"}
    return build_context(target_event=event, now=_NOW)


def _add_evidence(session: object) -> EvidenceItem:
    item = EvidenceItem(
        id="ev_c010_test",
        kind=EvidenceKind.CI_TEST_RESULT,
        title="CI passed",
        description="All checks passed.",
        source_system_id="sys_c010_test",
        source_ref="https://ci.example.invalid/runs/1",
        scope=Scope(system_id="sys_c010_test", artifact="demo", commit="abc123"),
        captured_at=_NOW,
        valid_from=_NOW,
        integrity_method=IntegrityMethod.HASH,
        relevance_to_finding="fnd_c010_test",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )
    return EvidenceRepository(session).add(item)  # type: ignore[arg-type]


def _persist_finding(
    session: object,
    *,
    status: FindingStatus = FindingStatus.DETECTED,
    supporting: tuple[str, ...] = ("ev_c010_test",),
) -> Finding:
    """Persist a finding with its critic run so it round-trips through storage."""

    run = CriticRun(
        critic_id="critic_c010_test",
        critic_version="1.0.0",
        input_refs=["clm_c010_test"],
        started_at=_NOW,
        completed_at=_NOW,
        result=CriticResultStatus.FINDING,
        rationale="recorded rationale",
    )
    CriticRunRepository(session).add(run)  # type: ignore[arg-type]
    finding = Finding(
        title="Self-consistency test finding",
        summary="A finding exercised by C-010 tests.",
        category=FindingCategory.MISSING_VERIFICATION,
        severity=Severity.MEDIUM,
        scope=Scope(system_id="sys_c010_test", artifact="demo", commit="abc123"),
        affected_system_ids=["sys_c010_test"],
        claim_ids=["clm_c010_test"],
        evidence_ids=list(supporting),
        supporting_evidence_ids=list(supporting),
        reasoning=Reasoning(observations=["Something was observed."]),
        recommended_action=RecommendedAction(text="Investigate."),
        critic_runs=[run],
        status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )
    FindingRepository(session).add(finding)  # type: ignore[arg-type]
    CriticRunRepository(session).link_finding(run.id, finding.id)  # type: ignore[arg-type]
    return finding


def _drive_to_confirmed(session: Session, finding_id: str) -> None:
    """Drive a finding through the legal SPEC §6 path to 'confirmed'."""

    lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())  # type: ignore[arg-type]
    steps: list[tuple[FindingStatus, dict[str, str]]] = [
        (FindingStatus.EVIDENCE_PENDING, {}),
        (FindingStatus.CHALLENGED, {}),
        (
            FindingStatus.VERIFICATION_PENDING,
            {"rebuttal_bypass_policy": "objectively reproducible failed check"},
        ),
        (FindingStatus.CONFIRMED, {}),
    ]
    for index, (target, extra) in enumerate(steps):
        lifecycle.transition(
            TransitionRequest(
                finding_id=finding_id,
                target_status=target,
                actor=_ACTOR,
                request_id=f"req_c010_{index}",
                rationale="recorded rationale",
                **extra,
            )
        )


# ---------------------------------------------------------------------------
# Control: a fully consistent store passes the self-consistency critic.
# ---------------------------------------------------------------------------


def test_consistent_store_no_finding(session_factory: SessionFactory) -> None:
    with session_scope(session_factory) as session:
        evidence = _add_evidence(session)
        finding = _persist_finding(session, supporting=(evidence.id,))
        _drive_to_confirmed(session, finding.id)

    result = NyxSelfConsistencyCritic(session_factory).run(_context())  # type: ignore[arg-type]

    assert result.result is CriticResultStatus.NO_FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION


# ---------------------------------------------------------------------------
# Positive: a confirmed finding with no recorded transitions violates the
# documented storage model (status must derive from a recorded valid transition).
# ---------------------------------------------------------------------------


def test_confirmed_finding_without_recorded_transitions_fires(
    session_factory: SessionFactory,
) -> None:
    with session_scope(session_factory) as session:
        evidence = _add_evidence(session)
        _persist_finding(session, status=FindingStatus.CONFIRMED, supporting=(evidence.id,))

    result = NyxSelfConsistencyCritic(session_factory).run(_context())  # type: ignore[arg-type]

    assert result.result is CriticResultStatus.FINDING
    assert result.category is FindingCategory.OTHER
    assert result.severity is Severity.CRITICAL or result.severity is Severity.HIGH
    observations_text = " ".join(result.observations).lower()
    assert "no lifecycle transition is recorded" in observations_text


def test_violation_result_can_be_composed_into_a_finding(session_factory: SessionFactory) -> None:
    """A C-010 violation must survive finding composition.

    Found by driving the whole critic pack against a live NYX: C-010 returned a ``finding`` result
    that named no claim or event, so the composer rejected it. The rejection happened *outside*
    the registry's per-critic guard, so one inconsistent store turned a routine pack run into a
    500 -- the very condition C-010 exists to report became un-reportable.
    """

    with session_scope(session_factory) as session:
        evidence = _add_evidence(session)
        _persist_finding(session, status=FindingStatus.CONFIRMED, supporting=(evidence.id,))

    result = NyxSelfConsistencyCritic(session_factory).run(_scoped_context())  # type: ignore[arg-type]
    assert result.result is CriticResultStatus.FINDING

    with session_scope(session_factory) as session:
        run = CriticRunRepository(session).add(
            CriticRun(
                critic_id=result.critic_id,
                critic_version=result.critic_version,
                input_refs=["clm_c010_test"],
                started_at=_NOW,
                completed_at=_NOW,
                result=result.result,
                rationale=result.rationale,
            )
        )
        finding = compose_finding(session, result=result, run=run)

    assert finding.id
    assert result.event_ids or result.claim_ids


# ---------------------------------------------------------------------------
# Positive: an audit entry edited after it was written breaks the chain.
# ---------------------------------------------------------------------------


def test_tampered_audit_entry_fires(session_factory: SessionFactory) -> None:
    with session_scope(session_factory) as session:
        evidence = _add_evidence(session)
        finding = _persist_finding(session, supporting=(evidence.id,))
        _drive_to_confirmed(session, finding.id)

    with session_scope(session_factory) as session:
        entries = AuditRepository(session).list(limit=10)
        assert entries, "consistent store must have recorded lifecycle transitions"
        target = session.get(AuditRow, entries[-1].id)
        assert target is not None
        target.reason = target.reason + " (edited after the fact)"

    result = NyxSelfConsistencyCritic(session_factory).run(_context())  # type: ignore[arg-type]

    assert result.result is CriticResultStatus.FINDING
    observations_text = " ".join(result.observations).lower()
    assert "entry_hash" in observations_text or "prior_entry_hash" in observations_text
