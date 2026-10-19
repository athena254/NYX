"""Unit tests for C-001 (CompletionVsVerificationCritic).

Covers acceptance scenarios:
  4 -- Same-commit passing evidence does NOT trigger C-001 (control).
  5 -- Mismatched commit DOES trigger a scoped finding.
  6 -- Missing verification creates a verification GAP, not a defect assertion.
  9 -- A finding cannot be `confirmed` without a rationale + evidence (composer).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nyx.critics.base import CriticResult
from nyx.critics.c001_completion_vs_verification import (
    CRITIC_ID,
    CRITIC_VERSION,
    CompletionVsVerificationCritic,
)
from nyx.critics.context import build_context
from nyx.domain.base import Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimType,
    CriticResultStatus,
    EvidenceKind,
    FindingCategory,
    FindingStatus,
    IntegrityMethod,
    Sensitivity,
    Severity,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem
from nyx.findings.composer import FindingCompositionError, compose_finding
from nyx.findings.models import CriticRun
from nyx.storage.repositories.critic_runs import CriticRunRepository

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _event() -> Event:
    return Event(
        source_system_id="sys_c001_test",
        event_type="agent.completion_claim",
        occurred_at=_NOW,
        correlation_id="c001-corr-1",
        payload={"claim_id": "clm_c001_test"},
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )


def _claim(commit: str) -> Claim:
    return Claim(
        statement="All required tests pass.",
        claimant_system_id="sys_c001_test",
        claim_type=ClaimType.COMPLETION,
        scope=Scope(system_id="sys_c001_test", artifact="demo", commit=commit),
        created_at=_NOW,
    )


def _evidence(
    *,
    commit: str,
    title: str = "CI run",
    description: str = "A CI test result for one commit.",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        kind=EvidenceKind.CI_TEST_RESULT,
        title=title,
        description=description,
        source_system_id="sys_c001_test",
        source_ref="https://ci.example.invalid/runs/1",
        scope=Scope(system_id="sys_c001_test", artifact="demo", commit=commit),
        captured_at=_NOW,
        valid_from=valid_from or _NOW,
        valid_until=valid_until,
        integrity_method=IntegrityMethod.HASH,
        relevance_to_claim="clm_c001_test",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )


def _scope() -> Scope:
    return Scope(system_id="sys_c001_test", artifact="demo", commit="abc123")


# ---------------------------------------------------------------------------
# Scenario 5 (positive): mismatched commit fires a finding.
# ---------------------------------------------------------------------------


def test_mismatched_commit_fires_finding() -> None:
    critic = CompletionVsVerificationCritic()
    claim = _claim(commit="abc123")
    evidence = _evidence(commit="def456")

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
    assert "abc123" in result.proposition
    assert result.observations, "finding must record observations"
    assert result.rationale, "finding must carry a rationale"


# ---------------------------------------------------------------------------
# Scenario 4 (control): same-commit passing evidence stays quiet.
# ---------------------------------------------------------------------------


def test_same_commit_passing_evidence_no_finding() -> None:
    critic = CompletionVsVerificationCritic()
    claim = _claim(commit="abc123")
    evidence = _evidence(
        commit="abc123",
        title="CI passed",
        description="All checks passed.",
        valid_until=None,
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING
    assert result.critic_id == CRITIC_ID
    assert result.rationale, "even a no_finding result must explain why"


# ---------------------------------------------------------------------------
# Scenario 6: missing verification produces a GAP, not a defect assertion.
# ---------------------------------------------------------------------------


def test_no_linked_verification_produces_gap_not_defect_claim() -> None:
    critic = CompletionVsVerificationCritic()
    claim = _claim(commit="abc123")

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[],  # nothing linked
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID

    # The finding must be a scoped verification gap.
    assert "verification" in result.proposition.lower() or "gap" in result.proposition.lower()

    # Its limitations must explicitly say the code is NOT being called defective.
    limitations_text = " ".join(result.limitations).lower()
    assert "not an assertion that the code is defective" in limitations_text or (
        "not" in limitations_text and "defective" in limitations_text
    ), (
        "scenario 6 requires the gap to explicitly state it is not a defect assertion; "
        f"got limitations: {result.limitations}"
    )

    # The rationale must also frame this as unknown, not broken.
    rationale_lower = result.rationale.lower()
    assert "unverified" in rationale_lower or "gap" in rationale_lower
    assert "not an accusation" in rationale_lower or "not" in rationale_lower


# ---------------------------------------------------------------------------
# Additional control: non-completion claim is out of scope.
# ---------------------------------------------------------------------------


def test_non_completion_claim_no_finding() -> None:
    critic = CompletionVsVerificationCritic()
    claim = Claim(
        statement="The system is slow.",
        claimant_system_id="sys_c001_test",
        claim_type=ClaimType.QUALITY,
        scope=_scope(),
        created_at=_NOW,
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        now=_NOW,
    )

    result = critic.run(context)
    assert result.result is CriticResultStatus.NO_FINDING


# ---------------------------------------------------------------------------
# Scenario 9: a finding cannot be `confirmed` without a rationale and at
# least one relevant evidence item. Driven through the composer.
# ---------------------------------------------------------------------------


def _make_critic_run(session: object) -> CriticRun:
    run = CriticRun(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        input_refs=["clm_c001_test"],
        started_at=_NOW,
        completed_at=_NOW,
        result=CriticResultStatus.FINDING,
        rationale="test rationale",
    )
    CriticRunRepository(session).add(run)  # type: ignore[arg-type]
    return run


def _make_finding_result(
    *,
    supporting_evidence_ids: list[str] | None = None,
    rationale: str = "A valid rationale for the finding.",
) -> CriticResult:
    return CriticResult(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        result=CriticResultStatus.FINDING,
        proposition="Test finding proposition.",
        scope=_scope(),
        observations=["Something was observed."],
        supporting_evidence_ids=supporting_evidence_ids or [],
        rationale=rationale,
        recommended_next_step="Investigate.",
        category=FindingCategory.MISSING_VERIFICATION,
        severity=Severity.MEDIUM,
        title="Test finding",
        summary="Test finding summary.",
        claim_ids=["clm_c001_test"],
        event_ids=[],
        affected_system_ids=["sys_c001_test"],
    )


def test_composer_rejects_confirmed_without_supporting_evidence(
    session_factory: object,
) -> None:
    """Scenario 9: no supporting evidence -> cannot be confirmed."""
    session = session_factory()  # type: ignore[operator]
    run = _make_critic_run(session)

    result = _make_finding_result(supporting_evidence_ids=[])

    with pytest.raises(FindingCompositionError, match="supporting evidence"):
        compose_finding(
            session,  # type: ignore[arg-type]
            result=result,
            run=run,
            requested_status=FindingStatus.CONFIRMED,
        )
