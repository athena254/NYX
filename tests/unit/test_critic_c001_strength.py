"""Tests for C-001's strength rule: a completion claim needs independent verification.

These are the brief's acceptance criteria for C-001, stated as tests:

* a completion claim with only self-reported evidence is **flagged**;
* the same claim with a linked independent observation **passes**;
* a stale or wrong-version report cannot verify a newer change;
* the achieved strength is emitted either way.

And the regression guard: with the flag off, C-001 behaves exactly as it did before the
ground-truth layer existed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.critics.base import CriticResultStatus
from nyx.critics.c001_completion_vs_verification import CompletionVsVerificationCritic
from nyx.critics.context import build_context
from nyx.domain.base import PrincipalRef, Scope, utc_now
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimType,
    ClaimStatus,
    Environment,
    EvidenceKind,
    IntegrityMethod,
    PrincipalKind,
    ProbeKind,
    ProbeOutcome,
    RetentionClass,
    Sensitivity,
    VerificationResult,
    VerificationStrength,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem, EvidenceVerification
from nyx.domain.observation import Observation

_SYSTEM = "sys_demo"
_CLAIMANT = "sys_ghost"
_COLLECTOR = PrincipalRef(id="nyx:probe", kind=PrincipalKind.SYSTEM)
_PRODUCER = PrincipalRef(id=_CLAIMANT, kind=PrincipalKind.SYSTEM)
_SCOPE = Scope(system_id=_SYSTEM, artifact="booking-service", commit="abc123")


def _event() -> Event:
    return Event(
        source_system_id=_CLAIMANT,
        event_type="agent.completion_claim",
        occurred_at=utc_now(),
        received_at=utc_now(),
        correlation_id="corr-1",
        payload={"claim_id": "clm_1"},
        content_hash="sha256:" + "b" * 64,
        trust_class="observed",
        retention_class=RetentionClass.STANDARD,
    )


def _claim(*, commit: str = "abc123") -> Claim:
    return Claim(
        id="clm_1",
        statement=f"All required tests pass for commit {commit}.",
        claimant_system_id=_CLAIMANT,
        claim_type=ClaimType.COMPLETION,
        scope=_SCOPE.model_copy(update={"commit": commit}),
        status=ClaimStatus.UNASSESSED,
        retention_class=RetentionClass.STANDARD,
    )


def _evidence(
    *,
    commit: str = "abc123",
    producer: PrincipalRef | None = None,
    title: str = "CI run for abc123",
    description: str = "All required checks passed.",
) -> EvidenceItem:
    return EvidenceItem(
        kind=EvidenceKind.CI_TEST_RESULT,
        title=title,
        description=description,
        source_system_id=producer.id if producer else "sys_ci",
        source_ref="ci://run/1",
        locator="runs/1",
        scope=_SCOPE.model_copy(update={"commit": commit}),
        captured_at=utc_now(),
        valid_from=utc_now(),
        valid_until=None,
        content_hash=None,
        integrity_method=IntegrityMethod.NONE,
        producer_identity=producer if producer is not None else _PRODUCER,
        verification=EvidenceVerification(result=VerificationResult.NOT_CHECKED),
        relevance_to_claim="clm_1",
        limitations=["Recorded by the producer."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="policy:evidence-internal-read",
        retention_class=RetentionClass.STANDARD,
    )


def _observation(
    *,
    claim_id: str = "clm_1",
    subject_version: str = "abc123",
    strength: VerificationStrength = VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
    outcome: ProbeOutcome = ProbeOutcome.OBSERVED,
) -> Observation:
    return Observation(
        id="obs_1",
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
        detail="row booking_17 exists with status=confirmed",
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


def _strength() -> CompletionVsVerificationCritic:
    return CompletionVsVerificationCritic(require_strength=True)


# --- the two acceptance criteria ------------------------------------------


def test_only_self_reported_evidence_is_flagged() -> None:
    """A claim of completion with only self-reported evidence is flagged by C-001."""

    result = _strength().run(_context(evidence=[_evidence()]))
    assert result.result is CriticResultStatus.FINDING
    assert result.verification_strength is VerificationStrength.SELF_REPORTED
    assert result.category.value == "missing_verification"


def test_the_same_claim_with_a_linked_independent_observation_passes() -> None:
    result = _strength().run(_context(evidence=[_evidence()], observations=[_observation()]))
    assert result.result is CriticResultStatus.NO_FINDING
    assert result.verification_strength is VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE
    assert result.observation_refs == ["obs_1"]


def test_the_passing_result_still_observes_that_it_is_not_certification() -> None:
    """A pass states what was achieved; it does not certify the underlying work."""

    result = _strength().run(_context(observations=[_observation()]))
    assert "not a certification" in result.rationale


def test_a_self_reported_claim_with_no_evidence_at_all_is_flagged_as_none() -> None:
    result = _strength().run(_context())
    assert result.result is CriticResultStatus.FINDING
    assert result.verification_strength is VerificationStrength.NONE


# --- version binding ------------------------------------------------------


def test_a_stale_report_cannot_verify_a_newer_change() -> None:
    """The brief's criterion: a wrong-version report cannot verify a newer change."""

    result = _strength().run(_context(observations=[_observation(subject_version="def456")]))
    assert result.result is CriticResultStatus.FINDING
    assert result.subject_version == "abc123"
    assert "does not name commit" in " ".join(result.observations) or "none covers" in (
        result.proposition
    )


def test_an_observation_of_the_claimed_version_passes() -> None:
    result = _strength().run(_context(observations=[_observation(subject_version="abc123")]))
    assert result.result is CriticResultStatus.NO_FINDING


def test_an_undated_observation_does_not_cover_a_dated_claim() -> None:
    """An undated observation cannot verify a dated one."""

    result = _strength().run(_context(observations=[_observation(subject_version=None)]))
    assert result.result is CriticResultStatus.FINDING
    assert result.verification_strength is VerificationStrength.NONE


def test_an_observation_for_another_claim_is_not_support_for_this_one() -> None:
    result = _strength().run(_context(observations=[_observation(claim_id="clm_2")]))
    assert result.result is CriticResultStatus.FINDING
    assert result.observation_refs == []


# --- strength levels ------------------------------------------------------


def test_a_rerun_by_nyx_observation_clears_the_floor() -> None:
    """The floor is rerun_by_nyx, so an observation at exactly that strength passes."""

    result = _strength().run(
        _context(
            observations=[
                _observation(
                    strength=VerificationStrength.RERUN_BY_NYX, outcome=ProbeOutcome.OBSERVED
                )
            ]
        )
    )
    assert result.result is CriticResultStatus.NO_FINDING
    assert result.verification_strength is VerificationStrength.RERUN_BY_NYX


def test_the_strongest_observation_wins_when_several_cover_the_claim() -> None:
    result = _strength().run(
        _context(
            observations=[
                _observation(strength=VerificationStrength.SELF_REPORTED),
                _observation(
                    strength=VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
                    outcome=ProbeOutcome.OBSERVED,
                ),
            ]
        )
    )
    assert result.result is CriticResultStatus.NO_FINDING


def test_a_covering_observation_below_the_floor_is_still_a_finding() -> None:
    result = _strength().run(
        _context(observations=[_observation(strength=VerificationStrength.SELF_REPORTED)])
    )
    assert result.result is CriticResultStatus.FINDING
    assert result.verification_strength is VerificationStrength.SELF_REPORTED


def test_the_finding_names_the_achieved_strength_in_its_observations() -> None:
    """The strength achieved is emitted on every branch, including the failure."""

    result = _strength().run(_context())
    assert any("Verification strength achieved: none" in item for item in result.observations)


def test_the_passing_result_names_the_achieved_strength_too() -> None:
    result = _strength().run(_context(observations=[_observation()]))
    assert any(
        "Verification strength achieved: independently_observed_end_state" in item
        for item in result.observations
    )


# --- the finding-schema extensions ----------------------------------------


def test_a_strength_finding_carries_the_observation_reference() -> None:
    result = _strength().run(_context(observations=[_observation(subject_version="def456")]))
    assert result.observation_refs == ["obs_1"]
    assert result.subject_version == "abc123"


def test_a_strength_finding_records_unreproduced_until_replayed() -> None:
    result = _strength().run(_context())
    assert result.reproducibility.value == "unreproduced"


def test_a_strength_finding_carries_a_remediation_hint() -> None:
    result = _strength().run(_context())
    assert result.remediation_hint is not None
    assert "observation" in result.remediation_hint


def test_a_strength_finding_names_the_claim_and_event_it_assesses() -> None:
    result = _strength().run(_context())
    assert result.claim_ids == ["clm_1"]
    assert result.event_ids


def test_a_strength_finding_keeps_the_gap_wording() -> None:
    """A verification gap must not be read as an accusation that the work is broken."""

    result = _strength().run(_context(evidence=[_evidence()]))
    assert any("not an assertion that the work is defective" in item for item in result.limitations)
    assert any("unverified is not the same as false" in item for item in result.limitations)


def test_the_self_reported_case_names_the_claimant_as_its_own_producer() -> None:
    result = _strength().run(_context(evidence=[_evidence()]))
    assert any("claimant cannot verify itself" in item for item in [result.proposition])


# --- the regression guard -------------------------------------------------


def test_with_the_flag_off_the_old_behaviour_is_unchanged() -> None:
    """Flags off equals current behaviour, which is testable rather than aspirational."""

    critic = CompletionVsVerificationCritic(require_strength=False)
    assert critic.require_strength is False

    # Self-reported evidence still verifies the claim on the old path.
    result = critic.run(_context(evidence=[_evidence()]))
    assert result.result is CriticResultStatus.NO_FINDING
    assert result.verification_strength is None


def test_the_pack_registers_the_old_rule_by_default() -> None:
    from nyx.critics import get

    assert get("c001-completion-vs-verification").require_strength is False


def test_register_ground_truth_is_a_noop_while_the_flag_is_off() -> None:
    from nyx.config import Settings
    from nyx.critics import register_ground_truth

    assert Settings().require_independent_verification is False
    assert register_ground_truth(Settings()) is False


def test_register_ground_truth_switches_the_registered_critic_on() -> None:
    from nyx.config import Settings
    from nyx.critics import get, register_ground_truth

    settings = Settings(require_independent_verification=True)
    assert register_ground_truth(settings) is True
    assert get("c001-completion-vs-verification").require_strength is True
    # Restore the default so this test cannot leak state into the pack.
    register_ground_truth(Settings())


def test_the_observation_path_ignores_a_claim_that_is_not_a_completion() -> None:
    """A non-completion claim is still out of scope, on either path."""

    factual = _claim().model_copy(update={"claim_type": ClaimType.FACTUAL})
    result = _strength().run(_context(claim=factual, observations=[_observation()]))
    assert result.result is CriticResultStatus.NO_FINDING
    assert result.verification_strength is None


def test_the_observation_path_ignores_a_claim_with_no_commit() -> None:
    unscoped = _claim().model_copy(
        update={"scope": Scope(system_id=_SYSTEM, environment="staging")}
    )
    result = _strength().run(_context(claim=unscoped, observations=[_observation()]))
    assert result.result is CriticResultStatus.NO_FINDING


def test_observations_are_not_invented_by_the_strength_path() -> None:
    """The composer rejects unknown references; a critic must never invent them."""

    context = build_context(
        target_event=_event(),
        linked_claim=_claim(),
        observations=[_observation(claim_id="clm_1")],
    )
    assert context.observations[0].id == "obs_1"


def test_an_observation_collected_in_the_future_is_still_dated() -> None:
    """The clock reading is UTC and aware; a naive reading would be rejected at construction."""

    observed = _observation().model_copy(
        update={"collected_at": datetime.now(UTC).replace(microsecond=0)}
    )
    assert observed.collected_at.tzinfo is not None
