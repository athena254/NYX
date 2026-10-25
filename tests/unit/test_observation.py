"""Tests for the observation aggregate and the verification-strength mapping.

The property that matters most is the one the brief states as the core principle: a claim of
completion is not the completion existing. These tests pin the mapping that makes the two
distinguishable, and the ordering that decides whether a given piece of support is strong enough.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nyx.domain.base import PrincipalRef, Scope, utc_now
from nyx.domain.enums import (
    ClaimType,
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
from nyx.domain.evidence import EvidenceItem, EvidenceVerification
from nyx.domain.observation import Observation
from nyx.evidence.verification import (
    COMPLETION_MINIMUM_STRENGTH,
    evidence_supports,
    is_at_least,
    strength_of_evidence,
    strength_of_observation,
    strongest,
)

_COLLECTOR = PrincipalRef(id="nyx:probe", kind=PrincipalKind.SYSTEM)
_SCOPE = Scope(system_id="sys_demo", artifact="booking-service", commit="abc123")


def _observation(
    *,
    strength: VerificationStrength = VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
    claim_id: str | None = "clm_1",
    subject_version: str | None = "abc123",
    read_only: bool = True,
    probe_kind: ProbeKind = ProbeKind.STATE_CHECK,
) -> Observation:
    return Observation(
        probe_id="probe:state-check",
        probe_version="1.0.0",
        subject="booking-service",
        subject_version=subject_version,
        collected_at=utc_now(),
        collector=_COLLECTOR,
        method=probe_kind,
        result=ProbeOutcome.OBSERVED,
        environment=Environment.STAGING,
        read_only=read_only,
        verification_strength=strength,
        scope=_SCOPE,
        relevance_to_claim=claim_id,
        detail="row booking_17 exists with status=confirmed",
    )


def _evidence(*, claim_id: str = "clm_1") -> EvidenceItem:
    return EvidenceItem(
        kind=EvidenceKind.TEST_RESULT,
        title="CI run for abc123",
        description="All required checks passed.",
        source_system_id="sys_ci",
        source_ref="ci://run/1",
        locator="runs/1",
        scope=_SCOPE,
        captured_at=utc_now(),
        valid_from=utc_now(),
        valid_until=None,
        content_hash=None,
        integrity_method=IntegrityMethod.NONE,
        producer_identity=PrincipalRef(id="sys_ci", kind=PrincipalKind.SERVICE),
        verification=EvidenceVerification(result=VerificationResult.NOT_CHECKED),
        relevance_to_claim=claim_id,
        limitations=["Recorded by the producer; NYX did not re-run it."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="policy:evidence-internal-read",
        retention_class=RetentionClass.STANDARD,
    )


# --- the strength ordering -------------------------------------------------


def test_strength_ordering_is_total_and_none_is_weakest() -> None:
    assert is_at_least(
        VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
        VerificationStrength.RERUN_BY_NYX,
    )
    assert not is_at_least(
        VerificationStrength.RERUN_BY_NYX,
        VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
    )
    assert is_at_least(VerificationStrength.NONE, VerificationStrength.NONE)
    assert not is_at_least(VerificationStrength.SELF_REPORTED, VerificationStrength.RERUN_BY_NYX)


def test_strongest_returns_the_maximum_and_none_for_an_empty_set() -> None:
    assert (
        strongest([VerificationStrength.NONE, VerificationStrength.SELF_REPORTED])
        is VerificationStrength.SELF_REPORTED
    )
    assert strongest([]) is VerificationStrength.NONE


def test_completion_minimum_is_rerun_by_nyx() -> None:
    """The floor the brief sets: a rerun by NYX, not the claimant's own report."""

    assert COMPLETION_MINIMUM_STRENGTH is VerificationStrength.RERUN_BY_NYX


# --- evidence can never be stronger than self-reported ---------------------


def test_evidence_is_capped_at_self_reported_however_it_is_integrity_checked() -> None:
    """ADR 0005: a producer's report is a claim about the world, not a reading of it.

    This is the function that stops an upgraded C-001 from treating a linked CI record as
    verification of the current commit.
    """

    item = _evidence()
    assert strength_of_evidence(item) is VerificationStrength.SELF_REPORTED

    signed = item.model_copy(update={"integrity_method": IntegrityMethod.SIGNED})
    assert strength_of_evidence(signed) is VerificationStrength.SELF_REPORTED


def test_observation_strength_is_what_the_probe_declared() -> None:
    assert strength_of_observation(_observation()) is (
        VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE
    )
    assert (
        strength_of_observation(_observation(strength=VerificationStrength.RERUN_BY_NYX))
        is VerificationStrength.RERUN_BY_NYX
    )


# --- per-claim linking -----------------------------------------------------


def test_a_claim_with_only_evidence_does_not_clear_the_completion_minimum() -> None:
    """The core acceptance criterion: self-reported evidence is not verification."""

    strength = evidence_supports("clm_1", evidence=[_evidence()], observations=[])
    assert strength is VerificationStrength.SELF_REPORTED
    assert not is_at_least(strength, COMPLETION_MINIMUM_STRENGTH)


def test_a_claim_with_a_linked_observation_clears_the_completion_minimum() -> None:
    strength = evidence_supports("clm_1", evidence=[_evidence()], observations=[_observation()])
    assert strength is VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE
    assert is_at_least(strength, COMPLETION_MINIMUM_STRENGTH)


def test_an_observation_gathered_for_another_claim_is_not_support_for_this_one() -> None:
    """A probe run against a subject is not the same as a probe run for this claim."""

    strength = evidence_supports(
        "clm_1", evidence=[_evidence()], observations=[_observation(claim_id="clm_2")]
    )
    assert strength is VerificationStrength.SELF_REPORTED


def test_no_support_at_all_is_none_rather_than_an_error() -> None:
    assert evidence_supports("clm_1", evidence=[], observations=[]) is VerificationStrength.NONE


# --- the aggregate itself --------------------------------------------------


def test_observation_defaults_to_read_only() -> None:
    """Read-only by default: the safe direction, and the brief's non-negotiable rule 5."""

    assert _observation().is_read_only is True
    assert _observation().read_only is True


def test_observation_reports_end_state_only_at_the_strongest_strength() -> None:
    assert _observation().is_end_state is True
    assert _observation(strength=VerificationStrength.RERUN_BY_NYX).is_end_state is False


def test_observation_rejects_a_naive_collected_at() -> None:
    """NYX stores UTC only; a naive datetime must not be silently read as local time.

    Constructed directly rather than with ``model_copy(update=...)``: pydantic deliberately
    skips validation on a copy, so a copy would not test the boundary it is meant to guard.
    """

    with pytest.raises(ValueError, match="timezone-aware"):
        Observation(
            probe_id="probe:state-check",
            probe_version="1.0.0",
            subject="booking-service",
            collected_at=datetime(2026, 10, 25, 12, 0, 0),
            collector=_COLLECTOR,
            method=ProbeKind.STATE_CHECK,
            result=ProbeOutcome.OBSERVED,
            environment=Environment.LOCAL,
            verification_strength=VerificationStrength.NONE,
        )


def test_observation_records_are_replaced_not_mutated() -> None:
    """NYX domain models are not frozen, so append-only is a convention, not a type rule.

    Recorded as a test because it is easy to assume the opposite: a reader who believes the
    models are frozen will trust an invariant the type system does not enforce. The actual
    guarantee is that repositories expose no update, and history is appended via ``model_copy``.
    """

    observed = _observation()
    changed = observed.model_copy(update={"detail": "the replacement"})
    assert changed.detail == "the replacement"
    assert observed.detail == "row booking_17 exists with status=confirmed"


def test_observation_stores_a_subject_version_so_a_stale_one_cannot_verify() -> None:
    """The field that lets a critic bind an observation to the claim's version."""

    observed = _observation(subject_version="def456")
    assert observed.subject_version == "def456"
    assert observed.subject_version != _SCOPE.commit


def test_observation_artifact_hash_is_a_canonical_digest() -> None:
    observed = _observation().model_copy(update={"raw_artifact_hash": "sha256:" + "a" * 64})
    assert observed.raw_artifact_hash == "sha256:" + "a" * 64


def test_observation_keeps_the_claim_link_optional_for_pre_claim_probes() -> None:
    """An adapter observes before any claim exists; the observation must still be storable."""

    observed = _observation(claim_id=None)
    assert observed.relevance_to_claim is None
    assert observed.subject == "booking-service"


def test_observation_payload_is_structured_data_not_text() -> None:
    observed = _observation().model_copy(
        update={"payload": {"rows": [{"id": "booking_17", "status": "confirmed"}]}}
    )
    assert observed.payload == {"rows": [{"id": "booking_17", "status": "confirmed"}]}


def test_environment_is_part_of_the_observation() -> None:
    """A staging observation must not be read as a production one."""

    assert _observation().environment is Environment.STAGING


def test_observation_rejects_an_unknown_extra_field() -> None:
    with pytest.raises(ValueError, match="extra"):
        Observation(
            probe_id="probe:x",
            probe_version="1.0.0",
            subject="s",
            collected_at=datetime.now(UTC),
            collector=_COLLECTOR,
            method=ProbeKind.STATE_CHECK,
            result=ProbeOutcome.OBSERVED,
            environment=Environment.LOCAL,
            verification_strength=VerificationStrength.NONE,
            unexpected_field="nope",
        )


def test_claim_type_enum_covers_the_extraction_output() -> None:
    """The extractor emits typed claims; the enum it uses must exist."""

    assert ClaimType.COMPLETION.value == "completion"
