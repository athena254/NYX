"""Unit tests for C-003 (EvidenceFreshnessCritic).

Covers acceptance scenario:
  8 -- Stale evidence cannot silently verify a newer artifact.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.critics.c003_evidence_freshness import (
    CRITIC_ID,
    CRITIC_VERSION,
    EvidenceFreshnessCritic,
)
from nyx.critics.context import build_context
from nyx.domain.base import Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimType,
    CriticResultStatus,
    EvidenceKind,
    IntegrityMethod,
    Sensitivity,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _event() -> Event:
    return Event(
        source_system_id="sys_c003_test",
        event_type="agent.completion_claim",
        occurred_at=_NOW,
        correlation_id="c003-corr-1",
        payload={"claim_id": "clm_c003_test"},
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )


def _claim(commit: str) -> Claim:
    return Claim(
        statement="All required tests pass.",
        claimant_system_id="sys_c003_test",
        claim_type=ClaimType.COMPLETION,
        scope=Scope(system_id="sys_c003_test", artifact="demo", commit=commit),
        created_at=_NOW,
    )


def _evidence(
    *,
    commit: str,
    captured_at: datetime,
    valid_from: datetime,
    valid_until: datetime | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        kind=EvidenceKind.CI_TEST_RESULT,
        title="CI run",
        description="A CI test result.",
        source_system_id="sys_c003_test",
        source_ref="https://ci.example.invalid/runs/1",
        scope=Scope(system_id="sys_c003_test", artifact="demo", commit=commit),
        captured_at=captured_at,
        valid_from=valid_from,
        valid_until=valid_until,
        integrity_method=IntegrityMethod.HASH,
        relevance_to_claim="clm_c003_test",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )


# ---------------------------------------------------------------------------
# Scenario 8 (positive): evidence outside validity window -> finding.
# ---------------------------------------------------------------------------


def test_evidence_expired_before_claim_fires_finding() -> None:
    critic = EvidenceFreshnessCritic()
    claim_time = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    claim = Claim(
        statement="All required tests pass.",
        claimant_system_id="sys_c003_test",
        claim_type=ClaimType.COMPLETION,
        scope=Scope(system_id="sys_c003_test", artifact="demo", commit="abc123"),
        created_at=claim_time,
    )

    # Evidence expired one hour before the claim was made.
    stale_evidence = _evidence(
        commit="abc123",
        captured_at=datetime(2026, 10, 19, 9, 0, tzinfo=UTC),
        valid_from=datetime(2026, 10, 19, 9, 0, tzinfo=UTC),
        valid_until=datetime(2026, 10, 19, 11, 0, tzinfo=UTC),
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[stale_evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION
    assert "window" in result.proposition.lower() or "validity" in result.proposition.lower()
    assert result.rationale
    assert result.observations


# ---------------------------------------------------------------------------
# Scenario 8 (control): evidence that covers the claim -> no finding.
# ---------------------------------------------------------------------------


def test_covering_evidence_no_finding() -> None:
    critic = EvidenceFreshnessCritic()
    claim_time = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    claim = Claim(
        statement="All required tests pass.",
        claimant_system_id="sys_c003_test",
        claim_type=ClaimType.COMPLETION,
        scope=Scope(system_id="sys_c003_test", artifact="demo", commit="abc123"),
        created_at=claim_time,
    )

    # Evidence captured before the claim, valid through and beyond it.
    covering = _evidence(
        commit="abc123",
        captured_at=datetime(2026, 10, 19, 11, 0, tzinfo=UTC),
        valid_from=datetime(2026, 10, 19, 11, 0, tzinfo=UTC),
        valid_until=datetime(2026, 10, 20, 0, 0, tzinfo=UTC),
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[covering],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING
    assert result.critic_id == CRITIC_ID
    assert result.rationale


# ---------------------------------------------------------------------------
# Additional positive: scope mismatch on commit -> finding.
# ---------------------------------------------------------------------------


def test_scope_mismatch_on_commit_fires_finding() -> None:
    critic = EvidenceFreshnessCritic()
    claim = _claim(commit="abc123")
    evidence = _evidence(
        commit="def456",
        captured_at=_NOW,
        valid_from=_NOW,
        valid_until=None,
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert "commit" in result.proposition.lower() or "scope" in result.proposition.lower()
