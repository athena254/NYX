"""Tests for C-016 (ClaimedVsObservedSideEffectsCritic).

The critic's one rule: the claimed effect of an action does not match the observed end state.

Acceptance coverage:

* TRUE NEGATIVE -- a claimed side effect with a confirming observation passes, and the pass is
  worded as agreement, not certification.
* TRUE POSITIVE -- a claimed side effect with no linked observation is flagged HIGH.
* TRUE POSITIVE -- a claimed side effect whose observation records ``not_observed`` is flagged HIGH.
* An observation for a different subject version does not confirm (MEDIUM).
* No claimed side effect means no finding: prose that does not assert one is never manufactured
  into a claim.
* The composer's evidence-reference rule is respected: an ``unsupported_claim`` finding always
  cites evidence it examined, and cited ids are never invented.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.critics.base import CriticResultStatus
from nyx.critics.c016_claimed_vs_observed_side_effects import (
    CRITIC_ID,
    CRITIC_VERSION,
    ClaimedVsObservedSideEffectsCritic,
)
from nyx.critics.context import build_context
from nyx.domain.base import PrincipalRef, Scope, utc_now
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimType,
    Environment,
    EvidenceKind,
    FindingCategory,
    FindingStatus,
    IntegrityMethod,
    PrincipalKind,
    ProbeKind,
    ProbeOutcome,
    RetentionClass,
    Sensitivity,
    Severity,
    VerificationStrength,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem, EvidenceVerification
from nyx.domain.observation import Observation

_SYSTEM = "sys_c016"
_CLAIMANT = "sys_ghost"
_COLLECTOR = PrincipalRef(id="nyx:probe", kind=PrincipalKind.SYSTEM)
_PRODUCER = PrincipalRef(id=_CLAIMANT, kind=PrincipalKind.SYSTEM)
_SCOPE = Scope(system_id=_SYSTEM, artifact="booking-service", commit="abc123")
_COMMIT = "abc123"


def _event() -> Event:
    return Event(
        source_system_id=_CLAIMANT,
        event_type="agent.completion_claim",
        occurred_at=utc_now(),
        received_at=utc_now(),
        correlation_id="c016-corr-1",
        payload={"claim_id": "clm_c016"},
        content_hash="sha256:" + "b" * 64,
        trust_class="observed",
        retention_class=RetentionClass.STANDARD,
    )


def _claim(
    *,
    statement: str = "I saved booking_17 to the bookings table.",
    commit: str = _COMMIT,
) -> Claim:
    return Claim(
        id="clm_c016",
        statement=statement,
        claimant_system_id=_CLAIMANT,
        claim_type=ClaimType.COMPLETION,
        scope=_SCOPE.model_copy(update={"commit": commit}),
        status="unassessed",
        retention_class=RetentionClass.STANDARD,
    )


def _evidence(
    *,
    claim_id: str = "clm_c016",
    description: str = "The agent reports that it wrote row booking_17.",
    commit: str = _COMMIT,
) -> EvidenceItem:
    return EvidenceItem(
        id="evd_c016",
        kind=EvidenceKind.LOG,
        title="Agent report for booking_17",
        description=description,
        source_system_id=_CLAIMANT,
        source_ref="log://agent/run/1",
        locator="runs/1",
        scope=_SCOPE.model_copy(update={"commit": commit}),
        captured_at=utc_now(),
        valid_from=utc_now(),
        valid_until=None,
        content_hash=None,
        integrity_method=IntegrityMethod.NONE,
        producer_identity=_PRODUCER,
        verification=EvidenceVerification(result="not_checked"),
        relevance_to_claim=claim_id,
        limitations=["Synthetic test evidence produced by the claimant."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="policy:evidence-internal-read",
        retention_class=RetentionClass.STANDARD,
    )


def _observation(
    *,
    claim_id: str = "clm_c016",
    subject_version: str | None = _COMMIT,
    outcome: ProbeOutcome = ProbeOutcome.OBSERVED,
    strength: VerificationStrength = VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
    detail: str = "row booking_17 exists in the bookings table with status=confirmed",
) -> Observation:
    return Observation(
        id="obs_c016",
        probe_id="probe:state-check",
        probe_version="1.0.0",
        subject="booking-service",
        subject_version=subject_version,
        collected_at=utc_now(),
        collector=_COLLECTOR,
        method=ProbeKind.STATE_CHECK,
        result=outcome,
        environment=Environment.STAGING,
        read_only=True,
        verification_strength=strength,
        relevance_to_claim=claim_id,
        detail=detail,
    )


def _context(
    *,
    claim: Claim | None = None,
    evidence: list[EvidenceItem] | None = None,
    observations: list[Observation] | None = None,
):
    return build_context(
        target_event=_event(),
        linked_claim=claim if claim is not None else _claim(),
        linked_evidence=list(evidence or []),
        observations=list(observations or []),
    )


def _critic() -> ClaimedVsObservedSideEffectsCritic:
    return ClaimedVsObservedSideEffectsCritic()


# --- TRUE NEGATIVE: a confirming observation passes ------------------------


def test_claimed_effect_with_a_confirming_observation_passes() -> None:
    """TRUE NEGATIVE: the claim and the observed end state agree."""

    result = _critic().run(_context(evidence=[_evidence()], observations=[_observation()]))

    assert result.result is CriticResultStatus.NO_FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION
    assert result.observation_refs == ["obs_c016"]
    assert result.subject_version == _COMMIT


def test_the_pass_states_agreement_not_certification() -> None:
    """A pass is agreement between claim and observation, never proof of cause."""

    result = _critic().run(_context(evidence=[_evidence()], observations=[_observation()]))

    assert "not a certification that this action caused the effect" in result.rationale


def test_the_pass_still_records_the_observation_it_examined() -> None:
    """A pass is inspectable: it names the observation that confirmed the effect."""

    result = _critic().run(_context(evidence=[_evidence()], observations=[_observation()]))

    assert result.observations, "a no_finding result must still record what was seen"
    assert result.verification_strength is VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE


# --- TRUE POSITIVE: no observation at all ----------------------------------


def test_claimed_effect_with_no_observation_is_flagged_high() -> None:
    """TRUE POSITIVE: a claimed effect nothing confirms is HIGH."""

    result = _critic().run(_context(evidence=[_evidence()], observations=[]))

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.HIGH
    assert result.category is FindingCategory.UNSUPPORTED_CLAIM
    assert result.observation_refs == []
    assert result.claim_ids == ["clm_c016"]
    assert result.event_ids


def test_a_claim_with_no_evidence_and_no_observation_is_a_scoped_gap() -> None:
    """With no evidence to cite the finding is a scoped gap, which the composer accepts."""

    result = _critic().run(_context(evidence=[], observations=[]))

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.HIGH
    assert result.category is FindingCategory.MISSING_VERIFICATION


# --- TRUE POSITIVE: the observed end state lacks the effect -----------------


def test_claimed_effect_with_a_not_observed_observation_is_flagged_high() -> None:
    """TRUE POSITIVE: NYX looked at the claimed version and the effect is not there."""

    result = _critic().run(
        _context(
            evidence=[_evidence()],
            observations=[_observation(outcome=ProbeOutcome.NOT_OBSERVED)],
        )
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.HIGH
    assert result.observation_refs == ["obs_c016"]
    assert result.subject_version == _COMMIT
    assert result.root_cause_key is not None


# --- version binding --------------------------------------------------------


def test_an_observation_for_a_different_version_does_not_confirm() -> None:
    """An observation of another commit cannot confirm this one (MEDIUM)."""

    result = _critic().run(
        _context(
            evidence=[_evidence()],
            observations=[_observation(subject_version="def456")],
        )
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.MEDIUM
    assert result.subject_version == _COMMIT
    assert "def456" in result.proposition


def test_an_undated_observation_does_not_confirm_a_dated_claim() -> None:
    result = _critic().run(
        _context(
            evidence=[_evidence()],
            observations=[_observation(subject_version=None)],
        )
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.subject_version == _COMMIT


def test_an_observation_for_another_claim_is_not_support_for_this_one() -> None:
    result = _critic().run(
        _context(
            evidence=[_evidence()],
            observations=[_observation(claim_id="clm_other")],
        )
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.HIGH
    assert result.observation_refs == []


def test_an_errored_probe_does_not_confirm_the_claimed_effect() -> None:
    """A probe that could not run is not a probe that found nothing."""

    result = _critic().run(
        _context(
            evidence=[_evidence()],
            observations=[
                _observation(
                    outcome=ProbeOutcome.ERROR,
                    detail="the probe timed out",
                    strength=VerificationStrength.NONE,
                )
            ],
        )
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.MEDIUM


# --- conservative detection --------------------------------------------------


def test_no_claimed_effect_means_no_finding() -> None:
    """TRUE NEGATIVE: prose that does not assert a side effect is not assessed."""

    result = _critic().run(
        _context(
            claim=_claim(statement="All required tests pass for commit abc123."),
            evidence=[_evidence(description="The suite is green.")],
            observations=[],
        )
    )

    assert result.result is CriticResultStatus.NO_FINDING
    assert result.observations


def test_neutral_prose_is_not_manufactured_into_a_side_effect_claim() -> None:
    """Words that merely mention a write without asserting one are not claims."""

    result = _critic().run(
        _context(
            claim=_claim(statement="The build ran and the report was generated."),
            evidence=[_evidence(description="The log file was read by the collector.")],
            observations=[],
        )
    )

    assert result.result is CriticResultStatus.NO_FINDING


def test_a_side_effect_asserted_only_in_linked_evidence_is_detected() -> None:
    """A producer may assert the effect in its report rather than in the claim statement."""

    result = _critic().run(
        _context(
            claim=_claim(statement="Booking handled."),
            evidence=[_evidence(description="The agent created a new booking record.")],
            observations=[],
        )
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.HIGH


def test_a_side_effect_asserted_only_in_the_claim_statement_is_detected() -> None:
    result = _critic().run(
        _context(
            claim=_claim(statement="I deleted the stale row from the bookings table."),
            evidence=[_evidence(description="Routine run, nothing to report.")],
            observations=[],
        )
    )

    assert result.result is CriticResultStatus.FINDING


# --- out of scope ------------------------------------------------------------


def test_no_linked_claim_is_out_of_scope() -> None:
    result = _critic().run(build_context(target_event=_event(), linked_claim=None, observations=[]))

    assert result.result is CriticResultStatus.NO_FINDING
    assert result.observations


def test_a_claim_without_a_commit_is_out_of_scope() -> None:
    """Without a commit there is no version to bind an observation to."""

    unscoped = _claim().model_copy(
        update={"scope": Scope(system_id=_SYSTEM, environment="staging")}
    )
    result = _critic().run(_context(claim=unscoped, observations=[_observation()]))

    assert result.result is CriticResultStatus.NO_FINDING


# --- the finding schema ------------------------------------------------------


def test_the_finding_carries_the_ground_truth_extensions() -> None:
    result = _critic().run(_context(evidence=[_evidence()], observations=[]))

    assert result.root_cause_key is not None
    assert result.root_cause_key.startswith("sha256:")
    assert len(result.root_cause_key) == len("sha256:") + 64
    assert result.subject_version == _COMMIT
    assert result.verification_strength is VerificationStrength.NONE
    assert result.reproducibility is not None


def test_the_root_cause_key_is_stable_for_one_claim() -> None:
    """Repeats of the same claim must dedupe to one finding, not raise a second."""

    first = _critic().run(_context(evidence=[_evidence()], observations=[]))
    second = _critic().run(_context(evidence=[_evidence()], observations=[]))

    assert first.root_cause_key == second.root_cause_key
    assert isinstance(first.root_cause_key, str)


def test_the_finding_names_the_claim_event_and_system_it_assesses() -> None:
    context = _context(evidence=[_evidence()], observations=[])
    result = _critic().run(context)

    assert result.claim_ids == ["clm_c016"]
    assert result.event_ids == [context.target_event.id]
    assert result.affected_system_ids == [_CLAIMANT]
    assert result.title is not None
    assert result.summary is not None


def test_the_finding_requires_human_review() -> None:
    result = _critic().run(_context(evidence=[_evidence()], observations=[]))

    assert result.requires_human_review is True


def test_the_finding_keeps_gap_wording_in_its_limitations() -> None:
    """A missing observation must not read as an accusation that the action failed."""

    result = _critic().run(_context(evidence=[_evidence()], observations=[]))

    limitations = " ".join(result.limitations).lower()
    assert "gap" in limitations
    assert "not proof that the action failed" in limitations


# --- the composer's evidence-reference rule ----------------------------------


def test_the_composers_evidence_reference_rule_is_respected(session_factory) -> None:
    """An ``unsupported_claim`` finding must cite evidence it examined, and cite only real ids.

    Driven through the real composer against a migrated database: the finding composes, which
    is the rule's actual purpose. A critic that emitted ``unsupported_claim`` with no evidence
    citation would raise here and take the whole pack run down with it.
    """

    from nyx.findings.composer import compose_finding
    from nyx.findings.models import CriticRun
    from nyx.storage.repositories.critic_runs import CriticRunRepository
    from nyx.storage.repositories.evidence import EvidenceRepository

    context = _context(evidence=[_evidence()], observations=[])
    result = _critic().run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.category is FindingCategory.UNSUPPORTED_CLAIM
    assert result.supporting_evidence_ids
    assert set(result.supporting_evidence_ids) <= {item.id for item in context.linked_evidence}

    session = session_factory()
    EvidenceRepository(session).add(_evidence())
    run = CriticRun(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        input_refs=["clm_c016"],
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        result=CriticResultStatus.FINDING,
        rationale=result.rationale,
    )
    CriticRunRepository(session).add(run)

    finding = compose_finding(session, result=result, run=run)

    assert finding.evidence_ids == ["evd_c016"]
    assert finding.observation_ids == []
    assert finding.subject_version == _COMMIT


def test_the_no_evidence_finding_composes_as_a_scoped_gap(session_factory) -> None:
    """The branch with nothing to cite must still compose: it is a gap, not an unsupported claim."""

    from nyx.findings.composer import compose_finding
    from nyx.findings.models import CriticRun
    from nyx.storage.repositories.critic_runs import CriticRunRepository

    context = _context(evidence=[], observations=[])
    result = _critic().run(context)

    assert result.category is FindingCategory.MISSING_VERIFICATION
    assert not result.supporting_evidence_ids

    session = session_factory()
    run = CriticRun(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        input_refs=["clm_c016"],
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        result=CriticResultStatus.FINDING,
        rationale=result.rationale,
    )
    CriticRunRepository(session).add(run)

    finding = compose_finding(session, result=result, run=run)

    assert finding.category is FindingCategory.MISSING_VERIFICATION
    assert finding.evidence_ids == []
    assert finding.status is FindingStatus.DETECTED
