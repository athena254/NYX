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
from nyx.domain.base import Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimType,
    CriticResultStatus,
    EvidenceKind,
    FindingCategory,
    IntegrityMethod,
    Sensitivity,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem

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
