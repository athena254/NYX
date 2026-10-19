"""Unit tests for C-005 (PolicyViolationCritic).

Covers acceptance scenario (SPEC §8):
  (policy) -- a record that violates a registered policy rule produces a finding that
  names the exact policy id, the policy version, the input record, and the decision;
  a compliant record produces no_finding.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.critics.c005_policy_violation import (
    CRITIC_ID,
    CRITIC_VERSION,
    PolicyViolationCritic,
)
from nyx.critics.context import build_context
from nyx.domain.base import PrincipalRef, Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimType,
    CriticResultStatus,
    EvidenceKind,
    FindingCategory,
    IntegrityMethod,
    PrincipalKind,
    Sensitivity,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _event() -> Event:
    return Event(
        source_system_id="sys_c005_test",
        event_type="agent.completion_claim",
        occurred_at=_NOW,
        correlation_id="c005-corr-1",
        payload={"claim_id": "clm_c005_test"},
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )


def _claim(*, claimant_principal_id: str | None = "user_alice") -> Claim:
    return Claim(
        statement="The deployment completed successfully.",
        claimant_system_id="sys_c005_test",
        claimant_principal_id=claimant_principal_id,
        claim_type=ClaimType.COMPLETION,
        scope=Scope(system_id="sys_c005_test", artifact="demo", commit="abc123"),
        created_at=_NOW,
    )


def _evidence(
    *,
    kind: EvidenceKind = EvidenceKind.CI_TEST_RESULT,
    producer: str | None = "svc_ci",
) -> EvidenceItem:
    return EvidenceItem(
        kind=kind,
        title="Linked evidence",
        description="Evidence linked to the completion claim.",
        source_system_id="sys_c005_test",
        source_ref="https://example.invalid/evidence/1",
        locator="https://example.invalid/evidence/1",
        scope=Scope(system_id="sys_c005_test", artifact="demo", commit="abc123"),
        captured_at=_NOW,
        valid_from=_NOW,
        valid_until=None,
        integrity_method=IntegrityMethod.HASH,
        producer_identity=(
            PrincipalRef(id=producer, kind=PrincipalKind.SERVICE, display_name=producer)
            if producer is not None
            else None
        ),
        relevance_to_claim="clm_c005_test",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )


# ---------------------------------------------------------------------------
# Positive: a violating record produces a finding naming policy id and version.
# ---------------------------------------------------------------------------


def test_missing_approval_produces_finding_naming_policy() -> None:
    critic = PolicyViolationCritic()
    claim = _claim()

    # Completion claim with verification evidence but no approval record:
    # approval.required-recorded is violated.
    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[_evidence(kind=EvidenceKind.CI_TEST_RESULT)],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION
    assert result.category is FindingCategory.POLICY_VIOLATION
    assert result.requires_human_review is True

    combined = " ".join(result.observations) + " " + result.rationale
    assert "approval.required-recorded" in combined
    assert "1.0.0" in combined
    assert "violated" in combined


def test_self_approval_produces_finding_naming_policy() -> None:
    critic = PolicyViolationCritic()
    claim = _claim(claimant_principal_id="user_alice")

    # Approval record produced by the claimant: approval.no-self-approval is violated.
    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[
            _evidence(kind=EvidenceKind.CI_TEST_RESULT),
            _evidence(kind=EvidenceKind.REVIEW_RECORD, producer="user_alice"),
        ],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING

    combined = " ".join(result.observations) + " " + result.rationale
    assert "approval.no-self-approval" in combined
    assert "1.0.0" in combined


# ---------------------------------------------------------------------------
# Control: a compliant record produces no_finding.
# ---------------------------------------------------------------------------


def test_compliant_record_produces_no_finding() -> None:
    critic = PolicyViolationCritic()
    claim = _claim(claimant_principal_id="user_alice")

    # Verification evidence plus an approval record from a different principal.
    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[
            _evidence(kind=EvidenceKind.TEST_RESULT),
            _evidence(kind=EvidenceKind.REVIEW_RECORD, producer="user_bob"),
        ],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING
    assert result.critic_id == CRITIC_ID
    assert result.observations == []


def test_no_linked_claim_produces_no_finding() -> None:
    critic = PolicyViolationCritic()

    context = build_context(target_event=_event(), now=_NOW)

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


# ---------------------------------------------------------------------------
# The finding's observations/rationale must contain the rule id AND its version.
# ---------------------------------------------------------------------------


def test_finding_observations_and_rationale_contain_rule_id_and_version() -> None:
    critic = PolicyViolationCritic()
    claim = _claim()

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[_evidence(kind=EvidenceKind.CI_TEST_RESULT)],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    for observation in result.observations:
        if "VIOLATED" in observation:
            assert "approval.required-recorded" in observation
            assert "1.0.0" in observation
    assert "approval.required-recorded" in result.rationale
    assert "1.0.0" in result.rationale
