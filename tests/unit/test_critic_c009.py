"""Unit tests for C-009 (EvidenceConflictCritic).

Covers acceptance scenario for C-009:
  -- Materially conflicting evidence (a passing and a failing record for the same
     defined test under a comparable declared scope) returns ``inconclusive`` and
     requests verification. The exact status is asserted -- a ``finding`` would mean
     the critic picked a winner, which is precisely the failure this guard prevents.
  -- Consistent evidence stays quiet.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.critics.c009_evidence_conflict import (
    CRITIC_ID,
    CRITIC_VERSION,
    EvidenceConflictCritic,
)
from nyx.critics.context import build_context
from nyx.domain.base import Scope
from nyx.domain.enums import (
    CriticResultStatus,
    EvidenceKind,
    FindingCategory,
    IntegrityMethod,
    Sensitivity,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_FIRST = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
_SECOND = datetime(2026, 10, 19, 11, 0, tzinfo=UTC)


def _event() -> Event:
    return Event(
        source_system_id="sys_c009_test",
        event_type="agent.verification_observed",
        occurred_at=_NOW,
        correlation_id="c009-corr-1",
        payload={},
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )


def _evidence(
    *,
    outcome: str,
    captured_at: datetime,
    environment: str = "staging",
    artifact: str = "demo",
) -> EvidenceItem:
    return EvidenceItem(
        kind=EvidenceKind.CI_TEST_RESULT,
        title="unit suite",
        description="All tests passed." if outcome == "passed" else "3 tests failed.",
        source_system_id="sys_c009_test",
        source_ref="https://ci.example.invalid/runs/1",
        scope=Scope(
            system_id="sys_c009_test",
            artifact=artifact,
            commit="abc123",
            environment=environment,
        ),
        captured_at=captured_at,
        valid_from=captured_at,
        integrity_method=IntegrityMethod.HASH,
        relevance_to_finding="fnd_c009_test",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )


# ---------------------------------------------------------------------------
# Positive: materially disagreeing evidence -> INCONCLUSIVE, never a verdict.
# ---------------------------------------------------------------------------


def test_conflicting_evidence_returns_inconclusive_not_finding() -> None:
    critic = EvidenceConflictCritic()
    passing = _evidence(outcome="passed", captured_at=_FIRST)
    failing = _evidence(outcome="failed", captured_at=_SECOND)

    context = build_context(
        target_event=_event(),
        linked_evidence=[passing, failing],
        now=_NOW,
    )

    result = critic.run(context)

    # Assert the EXACT status: the critic must abstain, not pick a winner.
    assert result.result is CriticResultStatus.INCONCLUSIVE
    assert result.result is not CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION
    assert result.category is FindingCategory.CONTRADICTION
    assert result.requires_human_review is True
    assert passing.id in result.supporting_evidence_ids
    assert failing.id in result.counterevidence_ids
    assert "does not pick a winner" in " ".join(result.observations)
    assert "inconclusive" in result.rationale.lower()
    assert "verification" in result.recommended_next_step.lower()


# ---------------------------------------------------------------------------
# Controls: consistent or non-comparable evidence stays quiet.
# ---------------------------------------------------------------------------


def test_consistent_passing_evidence_no_finding() -> None:
    critic = EvidenceConflictCritic()
    first = _evidence(outcome="passed", captured_at=_FIRST)
    second = _evidence(outcome="passed", captured_at=_SECOND)

    context = build_context(
        target_event=_event(),
        linked_evidence=[first, second],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_conflicting_evidence_in_different_environments_no_finding() -> None:
    """Different declared environments are not automatically in conflict."""

    critic = EvidenceConflictCritic()
    passing = _evidence(outcome="passed", captured_at=_FIRST, environment="staging")
    failing = _evidence(outcome="failed", captured_at=_SECOND, environment="production")

    context = build_context(
        target_event=_event(),
        linked_evidence=[passing, failing],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_conflicting_evidence_for_different_artifacts_no_finding() -> None:
    critic = EvidenceConflictCritic()
    passing = _evidence(outcome="passed", captured_at=_FIRST, artifact="demo")
    failing = _evidence(outcome="failed", captured_at=_SECOND, artifact="other")

    context = build_context(
        target_event=_event(),
        linked_evidence=[passing, failing],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING
