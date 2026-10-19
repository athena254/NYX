"""Unit tests for C-002 (ContradictoryRecordsCritic).

Covers acceptance scenario:
  7 -- Contradictory claims for DIFFERENT versions are NOT automatically treated
       as contradictory; only same-scope (artifact + commit + environment + period)
       contradictions fire.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.critics.c002_contradictory_records import (
    CRITIC_ID,
    CRITIC_VERSION,
    ContradictoryRecordsCritic,
)
from nyx.critics.context import build_context
from nyx.domain.base import Period, Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import ClaimType, CriticResultStatus
from nyx.domain.events import Event

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _event() -> Event:
    return Event(
        source_system_id="sys_c002_test",
        event_type="agent.completion_claim",
        occurred_at=_NOW,
        correlation_id="c002-corr-1",
        payload={"claim_id": "clm_c002_test"},
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )


def _claim(
    *,
    statement: str,
    commit: str,
    artifact: str = "demo",
    environment: str = "staging",
    period: Period | None = None,
    claimant: str = "sys_c002_test",
) -> Claim:
    return Claim(
        statement=statement,
        claimant_system_id=claimant,
        claim_type=ClaimType.FACTUAL,
        scope=Scope(
            system_id=claimant,
            artifact=artifact,
            commit=commit,
            environment=environment,
            period=period,
        ),
        created_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Scenario 7 (positive): same scope, opposite stances -> finding.
# ---------------------------------------------------------------------------


def test_same_scope_opposite_stances_fires_finding() -> None:
    critic = ContradictoryRecordsCritic()
    target = _claim(statement="All tests pass.", commit="abc123")
    other = _claim(statement="The build failed.", commit="abc123")

    context = build_context(
        target_event=_event(),
        linked_claim=target,
        related_claims=[other],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION
    assert "abc123" in result.proposition
    assert result.contradiction, "contradiction findings must record the contradiction"
    assert result.observations


# ---------------------------------------------------------------------------
# Scenario 7 (control): different commits -> no finding.
# ---------------------------------------------------------------------------


def test_different_commits_no_finding() -> None:
    critic = ContradictoryRecordsCritic()
    target = _claim(statement="All tests pass.", commit="abc123")
    other = _claim(statement="The build failed.", commit="def456")

    context = build_context(
        target_event=_event(),
        linked_claim=target,
        related_claims=[other],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


# ---------------------------------------------------------------------------
# Scenario 7 (control): different environments -> no finding.
# ---------------------------------------------------------------------------


def test_different_environments_no_finding() -> None:
    critic = ContradictoryRecordsCritic()
    target = _claim(statement="All tests pass.", commit="abc123", environment="staging")
    other = _claim(statement="The build failed.", commit="abc123", environment="production")

    context = build_context(
        target_event=_event(),
        linked_claim=target,
        related_claims=[other],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


# ---------------------------------------------------------------------------
# Scenario 7 (control): non-overlapping periods -> no finding.
# ---------------------------------------------------------------------------


def test_non_overlapping_periods_no_finding() -> None:
    critic = ContradictoryRecordsCritic()
    target = _claim(
        statement="All tests pass.",
        commit="abc123",
        period=Period(
            from_=datetime(2026, 1, 1, tzinfo=UTC),
            to=datetime(2026, 1, 31, tzinfo=UTC),
        ),
    )
    other = _claim(
        statement="The build failed.",
        commit="abc123",
        period=Period(
            from_=datetime(2026, 6, 1, tzinfo=UTC),
            to=datetime(2026, 6, 30, tzinfo=UTC),
        ),
    )

    context = build_context(
        target_event=_event(),
        linked_claim=target,
        related_claims=[other],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING
