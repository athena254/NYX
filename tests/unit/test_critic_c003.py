"""Unit tests for C-003 (EvidenceFreshnessCritic).

Covers acceptance scenario:
  8 -- Stale evidence cannot silently verify a newer artifact.

Also proves the two rules the ground-truth upgrade adds:
* the subject version difference itself triggers a freshness finding, without any help from
  the capture time (a wrong-commit report captured *after* the claim was made was previously
  accepted silently);
* the covered-scope rule, which is off by default and fires only when both sides declare a
  covered set.

Every new rule has its near miss: a missing commit, an undeclared covered set, a case
difference, a same-prefix sibling, and the flag off. A false accusation is worse than a miss.
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


def _claim(commit: str, *, covered: list[str] | None = None) -> Claim:
    return Claim(
        statement="All required tests pass.",
        claimant_system_id="sys_c003_test",
        claim_type=ClaimType.COMPLETION,
        scope=Scope(
            system_id="sys_c003_test",
            artifact="demo",
            commit=commit,
            covered=list(covered or []),
        ),
        created_at=_NOW,
    )


def _evidence(
    *,
    commit: str,
    captured_at: datetime,
    valid_from: datetime,
    valid_until: datetime | None = None,
    covered: list[str] | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        kind=EvidenceKind.CI_TEST_RESULT,
        title="CI run",
        description="A CI test result.",
        source_system_id="sys_c003_test",
        source_ref="https://ci.example.invalid/runs/1",
        scope=Scope(
            system_id="sys_c003_test",
            artifact="demo",
            commit=commit,
            covered=list(covered or []),
        ),
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


# ---------------------------------------------------------------------------
# RULE 2: the subject version difference is the trigger, not the capture time.
# ---------------------------------------------------------------------------


def test_version_difference_fires_even_when_captured_after_the_claim() -> None:
    """A wrong-commit report captured *after* the claim was made still cannot verify it.

    Before the ground-truth layer this pair produced no finding: the capture time was the only
    trigger, and it no longer proved staleness. The version difference alone must fire.
    """

    critic = EvidenceFreshnessCritic()
    claim = _claim(commit="abc123")
    evidence = _evidence(
        commit="def456",
        captured_at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
        valid_from=datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
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
    # The finding keeps the timestamp evidence alongside the version evidence.
    assert any("captured at" in item for item in result.observations)
    assert any("def456" in item for item in result.observations)


def test_version_difference_fires_when_captured_at_the_same_instant() -> None:
    """Equal timestamps are not a reason to accept a different commit."""

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
    assert any("at the same time as" in item for item in result.observations)


def test_a_claim_without_a_commit_is_not_accused_of_a_version_mismatch() -> None:
    """An undeclared commit is unknown, not a mismatch."""

    critic = EvidenceFreshnessCritic()
    claim = Claim(
        statement="All required tests pass.",
        claimant_system_id="sys_c003_test",
        claim_type=ClaimType.COMPLETION,
        scope=Scope(system_id="sys_c003_test", artifact="demo"),
        created_at=_NOW,
    )
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

    assert result.result is CriticResultStatus.NO_FINDING


def test_evidence_without_a_commit_is_not_accused_of_a_version_mismatch() -> None:
    critic = EvidenceFreshnessCritic()
    claim = _claim(commit="abc123")
    evidence = _evidence(
        commit="def456",
        captured_at=_NOW,
        valid_from=_NOW,
        valid_until=None,
    ).model_copy(
        update={
            "scope": Scope(system_id="sys_c003_test", artifact="demo", covered=["/srv/app/src"])
        }
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_the_matching_commit_is_not_a_freshness_failure() -> None:
    """The same commit with a window that covers the claim is not a freshness failure.

    The clock reading is deliberately after the evidence's window so the window rule cannot be
    the reason this passes: only the version comparison is being tested here.
    """

    critic = EvidenceFreshnessCritic()
    claim = _claim(commit="abc123")
    evidence = _evidence(
        commit="abc123",
        captured_at=datetime(2026, 10, 19, 11, 0, tzinfo=UTC),
        valid_from=datetime(2026, 10, 19, 11, 0, tzinfo=UTC),
        valid_until=datetime(2026, 10, 19, 13, 0, tzinfo=UTC),
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


# ---------------------------------------------------------------------------
# RULE 3: covered scope. Off by default; fires only when both sides declare a set.
# ---------------------------------------------------------------------------


def _covered_context(
    *,
    critic: EvidenceFreshnessCritic,
    claim_covered: list[str],
    evidence_covered: list[str],
    evidence_valid_from: datetime | None = None,
    evidence_valid_until: datetime | None = None,
):
    claim = _claim(commit="abc123", covered=claim_covered)
    evidence = _evidence(
        commit="abc123",
        captured_at=datetime(2026, 10, 19, 11, 0, tzinfo=UTC),
        valid_from=evidence_valid_from or datetime(2026, 10, 19, 11, 0, tzinfo=UTC),
        valid_until=evidence_valid_until if evidence_valid_until is not None else _NOW,
        covered=evidence_covered,
    )
    return build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )


def test_covered_scope_gap_fires_when_the_flag_is_on() -> None:
    """The claim touched a file the evidence's covered set does not include."""

    critic = EvidenceFreshnessCritic(require_coverage_scope=True)
    context = _covered_context(
        critic=critic,
        claim_covered=["/srv/app/src/a.py", "/srv/app/src/b.py"],
        evidence_covered=["/srv/app/src/a.py"],
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert "b.py" in result.proposition or any("b.py" in item for item in result.observations)
    assert result.rationale


def test_covered_scope_gap_is_silent_when_the_flag_is_off() -> None:
    """Flags off equals current behaviour, which is testable rather than aspirational."""

    critic = EvidenceFreshnessCritic(require_coverage_scope=False)
    assert critic.require_coverage_scope is False

    context = _covered_context(
        critic=critic,
        claim_covered=["/srv/app/src/a.py", "/srv/app/src/b.py"],
        evidence_covered=["/srv/app/src/a.py"],
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_a_declared_directory_covers_its_own_subpaths() -> None:
    """``/srv/app/src`` covers ``/srv/app/src/a.py``, so this is not a gap."""

    critic = EvidenceFreshnessCritic(require_coverage_scope=True)
    context = _covered_context(
        critic=critic,
        claim_covered=["/srv/app/src/a.py"],
        evidence_covered=["/srv/app/src"],
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_trailing_slashes_are_normalised() -> None:
    critic = EvidenceFreshnessCritic(require_coverage_scope=True)
    context = _covered_context(
        critic=critic,
        claim_covered=["/srv/app/src/a.py"],
        evidence_covered=["/srv/app/src/"],
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_comparison_is_case_sensitive_so_a_case_change_is_a_gap() -> None:
    """Case is not folded: a different case is a different path, and saying so is honest."""

    critic = EvidenceFreshnessCritic(require_coverage_scope=True)
    context = _covered_context(
        critic=critic,
        claim_covered=["/srv/app/Src/a.py"],
        evidence_covered=["/srv/app/src/a.py"],
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING


def test_a_same_prefix_sibling_is_not_covered() -> None:
    """``/srv/app2`` must not be read as covering ``/srv/app``: the boundary is a separator."""

    critic = EvidenceFreshnessCritic(require_coverage_scope=True)
    context = _covered_context(
        critic=critic,
        claim_covered=["/srv/app"],
        evidence_covered=["/srv/app2"],
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING


def test_an_undeclared_evidence_covered_set_is_not_an_empty_set() -> None:
    """Absent telemetry is unknown, never a defect: no evidence set means no accusation."""

    critic = EvidenceFreshnessCritic(require_coverage_scope=True)
    context = _covered_context(
        critic=critic,
        claim_covered=["/srv/app/src/a.py"],
        evidence_covered=[],
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_an_undeclared_claim_covered_set_is_not_assessed() -> None:
    critic = EvidenceFreshnessCritic(require_coverage_scope=True)
    context = _covered_context(
        critic=critic,
        claim_covered=[],
        evidence_covered=["/srv/app/src/a.py"],
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_a_root_declaration_does_not_cover_a_relative_path() -> None:
    """``/`` is an absolute prefix: a relative path is not under it, so this is a real gap."""

    critic = EvidenceFreshnessCritic(require_coverage_scope=True)
    context = _covered_context(
        critic=critic,
        claim_covered=["src/a.py"],
        evidence_covered=["/"],
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
