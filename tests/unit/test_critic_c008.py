"""Unit tests for C-008 (UnsupportedCertaintyCritic).

Covers acceptance scenario for C-008:
  -- Over-strong wording ("all tests pass") against a single linked verification run
     fires; the result is framed as an assessment, never as verification of truth.
  -- Wording proportionate to its evidence scope stays quiet.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.critics.c008_unsupported_certainty import (
    CRITIC_ID,
    CRITIC_VERSION,
    UnsupportedCertaintyCritic,
)
from nyx.critics.context import build_context
from nyx.domain.base import PrincipalRef, Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimType,
    CriticResultStatus,
    Environment,
    EvidenceKind,
    FindingCategory,
    IntegrityMethod,
    PrincipalKind,
    ProbeKind,
    ProbeOutcome,
    Sensitivity,
    VerificationStrength,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem
from nyx.domain.observation import Observation

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _event() -> Event:
    return Event(
        source_system_id="sys_c008_test",
        event_type="agent.completion_claim",
        occurred_at=_NOW,
        correlation_id="c008-corr-1",
        payload={},
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )


def _claim(statement: str) -> Claim:
    return Claim(
        statement=statement,
        claimant_system_id="sys_c008_test",
        claim_type=ClaimType.FACTUAL,
        scope=Scope(system_id="sys_c008_test", artifact="demo", commit="abc123"),
        created_at=_NOW,
    )


def _evidence() -> EvidenceItem:
    """A single unit test run: the thinnest evidence scope."""

    return EvidenceItem(
        kind=EvidenceKind.TEST_RESULT,
        title="unit test: discount parser",
        description="One unit test passed.",
        source_system_id="sys_c008_test",
        source_ref="https://ci.example.invalid/runs/1",
        scope=Scope(system_id="sys_c008_test", artifact="demo", commit="abc123"),
        captured_at=_NOW,
        valid_from=_NOW,
        integrity_method=IntegrityMethod.HASH,
        relevance_to_claim="clm_c008_test",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )


# ---------------------------------------------------------------------------
# Positive: universal wording backed by a single verification run fires.
# ---------------------------------------------------------------------------


def test_universal_wording_with_single_run_fires() -> None:
    critic = UnsupportedCertaintyCritic()
    claim = _claim("All tests pass.")
    evidence = _evidence()

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION
    assert result.category is FindingCategory.UNSUPPORTED_CLAIM
    assert evidence.id in result.supporting_evidence_ids
    # The output must be an assessment, never labelled verification of truth.
    rationale_and_limitations = (result.rationale + " " + " ".join(result.limitations)).lower()
    assert "assessment" in rationale_and_limitations
    assert "proof" in rationale_and_limitations and "not" in rationale_and_limitations
    assert result.rationale


# ---------------------------------------------------------------------------
# Control: wording proportionate to its evidence scope stays quiet.
# ---------------------------------------------------------------------------


def test_proportionate_wording_no_finding() -> None:
    critic = UnsupportedCertaintyCritic()
    claim = _claim("The discount parser unit test passes on commit abc123.")

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[_evidence()],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING
    assert result.critic_id == CRITIC_ID


def test_universal_wording_with_multiple_runs_no_finding() -> None:
    critic = UnsupportedCertaintyCritic()
    claim = _claim("All tests pass.")
    first = _evidence()
    second = _evidence()

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[first, second],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_universal_wording_without_evidence_no_finding() -> None:
    """No attached evidence scope -> out of scope (missing verification is C-001's)."""

    critic = UnsupportedCertaintyCritic()
    claim = _claim("All tests pass.")

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


# ---------------------------------------------------------------------------
# RULE 3: a stated confidence above the ceiling its support can carry.
# ---------------------------------------------------------------------------


def _claim_id(statement: str, claim_id: str = "clm_c008_test") -> Claim:
    claim = _claim(statement)
    claim.id = claim_id
    return claim


def _observation(
    *,
    strength: VerificationStrength = VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
    claim_id: str = "clm_c008_test",
    observation_id: str = "obs_c008_test",
) -> Observation:
    return Observation(
        id=observation_id,
        probe_id="probe_state_check",
        probe_version="1.0.0",
        subject="sys_c008_test",
        subject_version="abc123",
        collected_at=_NOW,
        collector=PrincipalRef(id="probe_state_check", kind=PrincipalKind.TOOL),
        method=ProbeKind.STATE_CHECK,
        result=ProbeOutcome.OBSERVED,
        environment=Environment.STAGING,
        read_only=True,
        verification_strength=strength,
        relevance_to_claim=claim_id,
    )


def _run_rule3(
    statement: str,
    *,
    evidence: list[EvidenceItem] | None = None,
    observations: list[Observation] | None = None,
):
    """Run RULE 3 with the claim linked to whatever support the test supplies."""

    event = _event()
    event.payload = {"confidence": 0}
    context = build_context(
        target_event=event,
        linked_claim=_claim_id(statement),
        linked_evidence=evidence if evidence is not None else [_evidence()],
        observations=observations or [],
        now=_NOW,
    )
    return UnsupportedCertaintyCritic().run(context)


def test_confidence_above_the_self_reported_ceiling_fires() -> None:
    """98% claimed with only the claimant's own report behind it."""

    result = _run_rule3("The migration is 98% complete and safe to ship.")

    assert result.result is CriticResultStatus.FINDING
    assert "98" in result.proposition
    assert "self_reported" in " ".join(result.observations)


def test_confidence_exactly_at_the_ceiling_passes() -> None:
    """Near miss: 50% against `self_reported` is at the ceiling, not above it."""

    result = _run_rule3("The migration is 50% complete.")

    assert result.result is CriticResultStatus.NO_FINDING


def test_confidence_below_the_ceiling_passes() -> None:
    result = _run_rule3("The migration is 20% complete.")

    assert result.result is CriticResultStatus.NO_FINDING


def test_claim_with_no_stated_confidence_is_not_assessed() -> None:
    """RULE 3 has nothing to compare when the claim states no number."""

    result = _run_rule3("The migration is underway.")

    assert result.result is CriticResultStatus.NO_FINDING


def test_stronger_support_raises_the_ceiling() -> None:
    """An end-state observation lifts the ceiling, so the same number now passes."""

    result = _run_rule3(
        "The migration is 98% complete and safe to ship.",
        observations=[_observation(strength=VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE)],
    )

    assert result.result is CriticResultStatus.NO_FINDING


def test_rule3_with_nothing_linked_is_recorded_as_a_verification_gap() -> None:
    """Regression: RULE 3 raised an `unsupported_claim` finding that cited no evidence.

    The composer refuses an ``unsupported_claim`` finding with nothing examined, and the
    eval runner died on it with ``FindingCompositionError``. With no linked evidence there is
    nothing to cite, so the finding must be a scoped verification gap instead -- the same
    split C-001 makes.
    """

    event = _event()
    event.payload = {"confidence": 99}
    context = build_context(
        target_event=event,
        linked_claim=_claim_id("The migration is complete."),
        linked_evidence=[],
        observations=[],
        now=_NOW,
    )

    result = UnsupportedCertaintyCritic().run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.category is FindingCategory.MISSING_VERIFICATION
    assert result.supporting_evidence_ids == []


def test_rule3_cites_the_evidence_it_examined() -> None:
    """Near miss: with evidence linked, the finding cites it rather than switching category."""

    linked = _evidence()
    event = _event()
    event.payload = {"confidence": 0}
    context = build_context(
        target_event=event,
        linked_claim=_claim_id("The migration is 98% complete and safe to ship."),
        linked_evidence=[linked],
        observations=[],
        now=_NOW,
    )

    result = UnsupportedCertaintyCritic().run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.category is FindingCategory.UNSUPPORTED_CLAIM
    assert result.supporting_evidence_ids == [linked.id]


def test_structural_confidence_field_is_read() -> None:
    """A producer that records the number structurally is read too."""

    event = _event()
    event.payload = {"confidence": 99}
    context = build_context(
        target_event=event,
        linked_claim=_claim_id("The migration is complete."),
        linked_evidence=[],
        observations=[],
        now=_NOW,
    )

    result = UnsupportedCertaintyCritic().run(context)

    assert result.result is CriticResultStatus.FINDING


def test_unlinked_observation_does_not_back_the_claim() -> None:
    """A strong observation gathered for another claim is not support for this one."""

    result = _run_rule3(
        "The migration is 98% complete and safe to ship.",
        observations=[
            _observation(
                strength=VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
                claim_id="clm_some_other_claim",
            )
        ],
    )

    assert result.result is CriticResultStatus.FINDING
