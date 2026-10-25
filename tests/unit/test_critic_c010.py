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

from nyx.critics.base import CriticResult
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


# ---------------------------------------------------------------------------
# The meta-findings: disagreements between rows NYX itself wrote.
# ---------------------------------------------------------------------------

_ROOT_CAUSE = "sha256:" + "a" * 64


def _persist_flagged(
    session: object,
    *,
    finding_id: str,
    severity: Severity = Severity.MEDIUM,
    status: FindingStatus = FindingStatus.DETECTED,
    root_cause_key: str | None = None,
    duplicate_of: str | None = None,
    observation_ids: tuple[str, ...] = (),
) -> Finding:
    """Persist a finding carrying the ground-truth extension columns."""

    run = CriticRun(
        id=f"run_{finding_id}",
        critic_id="critic_meta_probe",
        critic_version="1.0.0",
        input_refs=["clm_c010_test"],
        started_at=_NOW,
        completed_at=_NOW,
        result=CriticResultStatus.FINDING,
        rationale="recorded rationale",
    )
    CriticRunRepository(session).add(run)  # type: ignore[arg-type]

    finding = Finding(
        id=finding_id,
        title=f"Self-consistency probe {finding_id}",
        summary="A finding exercised by C-010's meta-finding tests.",
        category=FindingCategory.MISSING_VERIFICATION,
        severity=severity,
        scope=Scope(system_id="sys_c010_test", artifact="demo", commit="abc123"),
        affected_system_ids=["sys_c010_test"],
        claim_ids=["clm_c010_test"],
        evidence_ids=[],
        reasoning=Reasoning(observations=["Something was observed."]),
        recommended_action=RecommendedAction(text="Investigate."),
        status=status,
        critic_runs=[run],
        root_cause_key=root_cause_key,
        duplicate_of=duplicate_of,
        observation_ids=list(observation_ids),
        created_at=_NOW,
        updated_at=_NOW,
    )
    FindingRepository(session).add(finding)  # type: ignore[arg-type]
    CriticRunRepository(session).link_finding(run.id, finding.id)  # type: ignore[arg-type]
    return finding


def _persist_run(
    session: object,
    *,
    run_id: str,
    critic_id: str = "critic_meta_probe",
    critic_version: str = "1.0.0",
    critic_type: str = "deterministic",
    prompt_version: str | None = None,
) -> CriticRun:
    run = CriticRun(
        id=run_id,
        critic_id=critic_id,
        critic_type=critic_type,
        critic_version=critic_version,
        prompt_version=prompt_version,
        input_refs=["clm_c010_test"],
        started_at=_NOW,
        completed_at=_NOW,
        result=CriticResultStatus.NO_FINDING,
        rationale="recorded rationale",
    )
    CriticRunRepository(session).add(run)  # type: ignore[arg-type]
    return run


def _run_c010(session_factory: SessionFactory) -> CriticResult:
    return NyxSelfConsistencyCritic(session_factory).run(  # type: ignore[arg-type]
        _scoped_context()
    )


def _violations(session_factory: SessionFactory) -> list[str]:
    """Assert the store is inconsistent, and return the reported violations."""

    result = _run_c010(session_factory)
    assert result.result is CriticResultStatus.FINDING, result.proposition
    return list(result.observations)


def _assert_clean(session_factory: SessionFactory) -> None:
    """Assert the store passes every one of C-010's checks."""

    result = _run_c010(session_factory)
    assert result.result is CriticResultStatus.NO_FINDING, list(result.observations)


# -- duplicate root causes ----------------------------------------------------


def test_two_active_findings_on_one_root_cause_fire(
    session_factory: SessionFactory,
) -> None:
    """One defect recorded twice, with no duplicate_of link between the two rows."""

    with session_scope(session_factory) as session:
        _persist_flagged(session, finding_id="fnd_dup_a", root_cause_key=_ROOT_CAUSE)
        _persist_flagged(session, finding_id="fnd_dup_b", root_cause_key=_ROOT_CAUSE)

    found = " ".join(_violations(session_factory))

    assert "one defect is recorded twice" in found
    assert "fnd_dup_a" in found and "fnd_dup_b" in found


def test_a_recorded_duplicate_of_link_is_not_a_violation(
    session_factory: SessionFactory,
) -> None:
    """Near miss: the store already knows which row is the keeper."""

    with session_scope(session_factory) as session:
        _persist_flagged(session, finding_id="fnd_keep", root_cause_key=_ROOT_CAUSE)
        _persist_flagged(
            session,
            finding_id="fnd_secondary",
            root_cause_key=_ROOT_CAUSE,
            duplicate_of="fnd_keep",
        )

    _assert_clean(session_factory)


def test_a_withdrawn_finding_is_not_a_second_accusation(
    session_factory: SessionFactory,
) -> None:
    """Near miss: a rejected row is not a live second accusation of the same defect.

    The withdrawn row is driven through the lifecycle rather than written directly, because
    C-010's transition check requires every non-detected status to have a recorded history.
    Writing the status by hand would be asserting that the *transition* check is clean.
    """

    with session_scope(session_factory) as session:
        _persist_flagged(session, finding_id="fnd_live", root_cause_key=_ROOT_CAUSE)
        withdrawn = _persist_flagged(
            session,
            finding_id="fnd_withdrawn",
            root_cause_key=_ROOT_CAUSE,
            status=FindingStatus.DETECTED,
        )
        lifecycle = FindingLifecycle(  # type: ignore[arg-type]
            session, capability_checker=_AllowAllChecker()
        )
        lifecycle.transition(
            TransitionRequest(
                finding_id=withdrawn.id,
                target_status=FindingStatus.WITHDRAWN,
                actor=_ACTOR,
                request_id="req_c010_withdraw",
                rationale="recorded rationale",
            )
        )

    _assert_clean(session_factory)


# -- contradictory severities -------------------------------------------------


def test_contradictory_severities_for_one_root_cause_fire(
    session_factory: SessionFactory,
) -> None:
    with session_scope(session_factory) as session:
        _persist_flagged(
            session,
            finding_id="fnd_sev_low",
            severity=Severity.LOW,
            root_cause_key=_ROOT_CAUSE,
        )
        _persist_flagged(
            session,
            finding_id="fnd_sev_high",
            severity=Severity.HIGH,
            root_cause_key=_ROOT_CAUSE,
        )

    found = " ".join(_violations(session_factory))

    assert "contradictory severities" in found


def test_one_severity_per_root_cause_passes(session_factory: SessionFactory) -> None:
    """Near miss: agreeing severities are not a violation.

    The second row is also recorded as the first one's duplicate, because two active rows on
    one root cause is itself a violation -- without the link this test would be asserting
    that the *duplicate* check is clean rather than the severity check.
    """

    with session_scope(session_factory) as session:
        _persist_flagged(session, finding_id="fnd_agree_a", root_cause_key=_ROOT_CAUSE)
        _persist_flagged(
            session,
            finding_id="fnd_agree_b",
            root_cause_key=_ROOT_CAUSE,
            duplicate_of="fnd_agree_a",
        )

    _assert_clean(session_factory)


# -- observation references ---------------------------------------------------


def test_observation_reference_that_does_not_resolve_fires(
    session_factory: SessionFactory,
) -> None:
    """A finding citing a probe result NYX never stored is not evidence-linked."""

    with session_scope(session_factory) as session:
        _persist_flagged(session, finding_id="fnd_obs_missing", observation_ids=("obs_absent",))

    found = " ".join(_violations(session_factory))

    assert "observation ids that do not exist" in found
    assert "obs_absent" in found


def test_a_resolving_observation_reference_passes(session_factory: SessionFactory) -> None:
    """Near miss: an observation NYX did store is a valid reference."""

    from nyx.domain.observation import Observation
    from nyx.storage.repositories.observations import ObservationRepository

    with session_scope(session_factory) as session:
        ObservationRepository(session).add(  # type: ignore[arg-type]
            Observation(
                id="obs_present",
                probe_id="probe_state_check",
                probe_version="1.0.0",
                subject="sys_c010_test",
                collected_at=_NOW,
                collector=_ACTOR,
                method="state_check",
                result="observed",
                environment="staging",
                read_only=True,
                verification_strength="independently_observed_end_state",
            )
        )
        _persist_flagged(session, finding_id="fnd_obs_present", observation_ids=("obs_present",))

    _assert_clean(session_factory)


# -- version drift ------------------------------------------------------------


def test_critic_version_drift_fires(session_factory: SessionFactory) -> None:
    """One critic id running under two rule versions breaks result attribution."""

    with session_scope(session_factory) as session:
        _persist_run(session, run_id="run_v1", critic_version="1.0.0")
        _persist_run(session, run_id="run_v2", critic_version="2.0.0")

    found = " ".join(_violations(session_factory))

    assert "different critic versions" in found


def test_one_critic_version_passes(session_factory: SessionFactory) -> None:
    """Near miss: a single version for one critic id is not drift."""

    with session_scope(session_factory) as session:
        _persist_run(session, run_id="run_a", critic_version="1.0.0")
        _persist_run(session, run_id="run_b", critic_version="1.0.0")

    _assert_clean(session_factory)


def test_judge_drift_fires(session_factory: SessionFactory) -> None:
    """A model critic running under two prompt versions is not comparable across runs."""

    with session_scope(session_factory) as session:
        _persist_run(session, run_id="run_p1", critic_type="llm", prompt_version="v1")
        _persist_run(session, run_id="run_p2", critic_type="llm", prompt_version="v2")

    found = " ".join(_violations(session_factory))

    assert "different judge configurations" in found


def test_one_judge_configuration_passes(session_factory: SessionFactory) -> None:
    """Near miss: one prompt version is not drift."""

    with session_scope(session_factory) as session:
        _persist_run(session, run_id="run_q1", critic_type="llm", prompt_version="v1")
        _persist_run(session, run_id="run_q2", critic_type="llm", prompt_version="v1")

    _assert_clean(session_factory)
