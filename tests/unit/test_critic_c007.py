"""Unit tests for C-007 (RepeatedFailureRegressionCritic).

Covers acceptance scenario for C-007:
  -- A genuine regression (same defined test, a change of declared commit, pass then
     fail) fires a clean regression finding.
  -- The SAME failure in a DIFFERENT environment still reports the pass-then-fail
     sequence, but the environment difference is surfaced as an explicit confounder
     in the output (never hidden, never presented as a clean regression).
  -- A pass then a fail at the SAME declared commit and environment is classified
     **flaky**, not a regression, and is recorded as `Reproducibility.FLAKY`.
  -- A failure signature that recurs at or above the threshold escalates severity.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.critics.c007_repeated_failure_regression import (
    CRITIC_ID,
    CRITIC_VERSION,
    RepeatedFailureRegressionCritic,
)
from nyx.critics.context import build_context
from nyx.domain.base import Scope
from nyx.domain.enums import (
    CriticResultStatus,
    EvidenceKind,
    FindingCategory,
    IntegrityMethod,
    Reproducibility,
    Sensitivity,
    Severity,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_PASS_TIME = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
_FAIL_TIME = datetime(2026, 10, 19, 11, 0, tzinfo=UTC)


def _event() -> Event:
    return Event(
        source_system_id="sys_c007_test",
        event_type="agent.verification_observed",
        occurred_at=_NOW,
        correlation_id="c007-corr-1",
        payload={},
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )


def _evidence(
    *,
    outcome: str,
    captured_at: datetime,
    commit: str | None = "abc123",
    environment: str = "staging",
) -> EvidenceItem:
    """One CI run of the same defined test, differing only in the recorded outcome."""

    return EvidenceItem(
        kind=EvidenceKind.CI_TEST_RESULT,
        title="unit suite",
        description="All tests passed." if outcome == "passed" else "3 tests failed.",
        source_system_id="sys_c007_test",
        source_ref="https://ci.example.invalid/runs/1",
        scope=Scope(
            system_id="sys_c007_test",
            artifact="demo",
            commit=commit,
            environment=environment,
        ),
        captured_at=captured_at,
        valid_from=captured_at,
        integrity_method=IntegrityMethod.HASH,
        relevance_to_finding="fnd_c007_test",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )


# ---------------------------------------------------------------------------
# Positive: same defined test, same commit and environment, pass -> fail.
# ---------------------------------------------------------------------------


def test_genuine_regression_fires_clean_finding() -> None:
    """A pass at a declared commit followed by a fail whose commit is undeclared.

    This is the clean-regression branch. A fail that declares a *different* commit takes the
    confounder branch instead, because the commit difference is a difference a reader must be
    shown. The undeclared-commit case is the one where nothing is comparable and nothing is
    hidden, so the critic reports a plain regression.
    """

    critic = RepeatedFailureRegressionCritic()
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME)
    failing = _evidence(outcome="failed", captured_at=_FAIL_TIME, commit=None)

    context = build_context(
        target_event=_event(),
        linked_evidence=[passing, failing],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION
    assert result.category is FindingCategory.QUALITY_RISK
    assert "regression" in result.proposition.lower()
    assert passing.id in result.proposition and failing.id in result.proposition
    observations_text = " ".join(result.observations).lower()
    assert "confounder" not in observations_text
    assert "classification: regression" in observations_text
    assert result.reproducibility is Reproducibility.DETERMINISTIC
    assert set(result.supporting_evidence_ids) == {passing.id, failing.id}


# ---------------------------------------------------------------------------
# Control: the SAME failure in a DIFFERENT environment must be reported with
# the environment difference as an explicit confounder.
# ---------------------------------------------------------------------------


def test_different_environment_reports_confounder() -> None:
    critic = RepeatedFailureRegressionCritic()
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME, environment="staging")
    failing = _evidence(outcome="failed", captured_at=_FAIL_TIME, environment="production")

    context = build_context(
        target_event=_event(),
        linked_evidence=[passing, failing],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    output_text = (
        result.proposition
        + " "
        + " ".join(result.observations)
        + " "
        + " ".join(result.limitations)
    ).lower()
    assert "confounder" in output_text, (
        "a changed environment must be surfaced as a confounder, not hidden; "
        f"got output: {output_text}"
    )
    assert "staging" in output_text and "production" in output_text
    assert "confounded" in output_text
    assert "not comparable" in output_text


def test_different_commit_reports_confounder() -> None:
    critic = RepeatedFailureRegressionCritic()
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME, commit="abc123")
    failing = _evidence(outcome="failed", captured_at=_FAIL_TIME, commit="def456")

    context = build_context(
        target_event=_event(),
        linked_evidence=[passing, failing],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    observations_text = " ".join(result.observations).lower()
    assert "confounder" in observations_text
    assert "abc123" in observations_text and "def456" in observations_text


# ---------------------------------------------------------------------------
# Controls: no prior pass, or a failure that already recovered, stays quiet.
# ---------------------------------------------------------------------------


def test_failure_without_prior_pass_no_finding() -> None:
    critic = RepeatedFailureRegressionCritic()
    context = build_context(
        target_event=_event(),
        linked_evidence=[_evidence(outcome="failed", captured_at=_FAIL_TIME)],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_failure_then_pass_is_not_a_regression() -> None:
    critic = RepeatedFailureRegressionCritic()
    failing = _evidence(outcome="failed", captured_at=_PASS_TIME)
    passing = _evidence(outcome="passed", captured_at=_FAIL_TIME)

    context = build_context(
        target_event=_event(),
        linked_evidence=[failing, passing],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_pass_fail_for_different_tested_artifacts_no_finding() -> None:
    critic = RepeatedFailureRegressionCritic()
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME)
    failing = EvidenceItem(
        kind=EvidenceKind.CI_TEST_RESULT,
        title="smoke suite",
        description="3 tests failed.",
        source_system_id="sys_c007_test",
        source_ref="https://ci.example.invalid/runs/2",
        scope=Scope(
            system_id="sys_c007_test",
            artifact="demo",
            commit="abc123",
            environment="staging",
        ),
        captured_at=_FAIL_TIME,
        valid_from=_FAIL_TIME,
        integrity_method=IntegrityMethod.HASH,
        relevance_to_finding="fnd_c007_test",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )

    context = build_context(
        target_event=_event(),
        linked_evidence=[passing, failing],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


# ---------------------------------------------------------------------------
# Classification: flaky vs regression.
# ---------------------------------------------------------------------------


def test_same_version_pass_then_fail_is_flaky() -> None:
    """Same declared commit AND environment, disagreeing outcomes -> flaky, not a regression.

    The artifact did not change and the environment did not change, so the disagreement is
    intermittent. Before the ground-truth upgrade this was reported as a plain regression,
    which overstates it.
    """

    critic = RepeatedFailureRegressionCritic()
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME)
    failing = _evidence(outcome="failed", captured_at=_FAIL_TIME)

    result = critic.run(
        build_context(target_event=_event(), linked_evidence=[passing, failing], now=_NOW)
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.reproducibility is Reproducibility.FLAKY
    assert result.severity is Severity.MEDIUM
    observations_text = " ".join(result.observations).lower()
    assert "classification: flaky" in observations_text
    assert "flaky" in result.proposition.lower()
    assert "confounder" not in observations_text


def test_flaky_classification_needs_both_scope_fields_declared() -> None:
    """A missing environment on either side cannot prove sameness, so it is not flaky.

    Only definitely-declared scope decides the classification. An undeclared field is a
    provenance gap, not evidence that the two runs are comparable.
    """

    critic = RepeatedFailureRegressionCritic()
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME, environment=None)
    failing = _evidence(outcome="failed", captured_at=_FAIL_TIME)

    result = critic.run(
        build_context(target_event=_event(), linked_evidence=[passing, failing], now=_NOW)
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.reproducibility is Reproducibility.DETERMINISTIC


def test_different_commits_are_a_regression_not_flaky() -> None:
    critic = RepeatedFailureRegressionCritic()
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME, commit="abc123")
    failing = _evidence(outcome="failed", captured_at=_FAIL_TIME, commit="def456")

    result = critic.run(
        build_context(target_event=_event(), linked_evidence=[passing, failing], now=_NOW)
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.reproducibility is Reproducibility.DETERMINISTIC


# ---------------------------------------------------------------------------
# Recurrence clustering and escalation.
# ---------------------------------------------------------------------------


def test_recurrence_at_threshold_escalates_severity() -> None:
    """Three failures sharing one signature inside the window escalate to CRITICAL."""

    critic = RepeatedFailureRegressionCritic(window_days=30.0, recurrence_threshold=3)
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME)
    failures = [
        _evidence(outcome="failed", captured_at=_FAIL_TIME),
        _evidence(outcome="failed", captured_at=_FAIL_TIME),
        _evidence(outcome="failed", captured_at=_FAIL_TIME),
    ]

    result = critic.run(
        build_context(target_event=_event(), linked_evidence=[passing, *failures], now=_NOW)
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.CRITICAL
    assert result.root_cause_key is not None and result.root_cause_key.startswith("sha256:")


def test_below_threshold_does_not_escalate() -> None:
    """Two recurrences against a threshold of three keep the base severity."""

    critic = RepeatedFailureRegressionCritic(recurrence_threshold=3)
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME)
    failures = [
        _evidence(outcome="failed", captured_at=_FAIL_TIME),
        _evidence(outcome="failed", captured_at=_FAIL_TIME),
    ]

    result = critic.run(
        build_context(target_event=_event(), linked_evidence=[passing, *failures], now=_NOW)
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.MEDIUM


def test_signature_clusters_runs_described_with_different_punctuation() -> None:
    """The same failure written two ways is one signature, so it recurs."""

    critic = RepeatedFailureRegressionCritic(recurrence_threshold=2)
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME)
    first = _evidence(outcome="failed", captured_at=_FAIL_TIME)
    second = _evidence(outcome="failed", captured_at=_FAIL_TIME)
    second.description = "3   tests -- failed!!"

    result = critic.run(
        build_context(target_event=_event(), linked_evidence=[passing, first, second], now=_NOW)
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.CRITICAL


def test_failures_outside_the_window_do_not_recur() -> None:
    """A long-dormant failure is not an ongoing cluster."""

    critic = RepeatedFailureRegressionCritic(window_days=1.0, recurrence_threshold=2)
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME)
    stale = _evidence(outcome="failed", captured_at=datetime(2026, 9, 1, 11, 0, tzinfo=UTC))
    recent = _evidence(outcome="failed", captured_at=_FAIL_TIME)

    result = critic.run(
        build_context(target_event=_event(), linked_evidence=[passing, stale, recent], now=_NOW)
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.MEDIUM


def test_two_distinct_signatures_are_not_clustered_together() -> None:
    critic = RepeatedFailureRegressionCritic(recurrence_threshold=2)
    passing = _evidence(outcome="passed", captured_at=_PASS_TIME)
    first = _evidence(outcome="failed", captured_at=_FAIL_TIME)
    second = _evidence(outcome="failed", captured_at=_FAIL_TIME)
    second.description = "timeout in the payment gateway"

    result = critic.run(
        build_context(target_event=_event(), linked_evidence=[passing, first, second], now=_NOW)
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.MEDIUM
