"""Unit tests for C-004 (ProvenanceGapCritic).

Covers acceptance scenario:
  (provenance) -- Evidence missing required provenance fields is flagged with the
  missing fields named; complete provenance is accepted.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.critics.c004_provenance_gap import (
    CRITIC_ID,
    CRITIC_VERSION,
    ProvenanceGapCritic,
)
from nyx.critics.context import build_context
from nyx.domain.base import PrincipalRef, Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimType,
    CriticResultStatus,
    EvidenceKind,
    IntegrityMethod,
    PrincipalKind,
    Sensitivity,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _event() -> Event:
    return Event(
        source_system_id="sys_c004_test",
        event_type="agent.completion_claim",
        occurred_at=_NOW,
        correlation_id="c004-corr-1",
        payload={"claim_id": "clm_c004_test"},
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )


def _claim() -> Claim:
    return Claim(
        statement="All required tests pass.",
        claimant_system_id="sys_c004_test",
        claim_type=ClaimType.COMPLETION,
        scope=Scope(system_id="sys_c004_test", artifact="demo", commit="abc123"),
        created_at=_NOW,
    )


def _evidence(
    *,
    source_system_id: str | None = "sys_c004_test",
    source_name: str | None = None,
    producer_identity: PrincipalRef | None = None,
    locator: str | None = "https://example.invalid/evidence/1",
    scope: Scope | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        kind=EvidenceKind.CI_TEST_RESULT,
        title="CI run",
        description="A CI test result.",
        source_system_id=source_system_id,
        source_name=source_name,
        source_ref="https://ci.example.invalid/runs/1",
        locator=locator,
        scope=scope,
        captured_at=_NOW,
        valid_from=_NOW,
        valid_until=None,
        integrity_method=IntegrityMethod.HASH,
        producer_identity=producer_identity,
        relevance_to_claim="clm_c004_test",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )


# ---------------------------------------------------------------------------
# Positive: evidence missing required provenance fields -> finding that names
# the missing fields and does not imply malice.
# ---------------------------------------------------------------------------


def test_missing_provenance_fires_finding_naming_fields() -> None:
    critic = ProvenanceGapCritic()
    claim = _claim()

    # Evidence with no producer identity, no locator, no scope.
    # (source_system_id is required by the model, so it cannot be missing.)
    incomplete = _evidence(
        producer_identity=None,
        locator=None,
        scope=None,
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[incomplete],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION

    # The finding must name the missing fields.
    all_observations = " ".join(result.observations).lower()
    all_limitations = " ".join(result.limitations).lower()
    combined = all_observations + " " + all_limitations + " " + result.rationale.lower()

    for field_name in ("locator", "scope"):
        assert field_name in combined, (
            f"provenance gap finding must name the missing field '{field_name}'; "
            f"got observations={result.observations} rationale={result.rationale}"
        )

    # Must not imply malice.
    assert "not an assertion of malicious behaviour" in all_limitations or (
        "not" in all_limitations and "malicious" in all_limitations
    ), "scenario 7 requires the provenance gap to explicitly not imply malice"


# ---------------------------------------------------------------------------
# Control: complete provenance -> no finding.
# ---------------------------------------------------------------------------


def test_complete_provenance_no_finding() -> None:
    critic = ProvenanceGapCritic()
    claim = _claim()

    complete = _evidence(
        source_system_id="sys_c004_test",
        source_name=None,
        producer_identity=PrincipalRef(
            id="proc_ci_agent",
            kind=PrincipalKind.SERVICE,
            display_name="CI Agent",
        ),
        locator="https://ci.example.invalid/runs/1",
        scope=Scope(system_id="sys_c004_test", artifact="demo", commit="abc123"),
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[complete],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING
    assert result.critic_id == CRITIC_ID
    assert result.rationale
