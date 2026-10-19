"""Unit tests for C-006 (RiskyActionWithoutAuthorisationCritic).

Covers SPEC §8 C-006 and safety invariant 6: NYX reports and records an unauthorised
high-impact action, and does NOT act on it.

The control tests matter most here. This critic must stay quiet for an ordinary action, for a
high-impact action that carries an approval, and when nothing is declared at all -- otherwise
it would accuse every routine operation of being unauthorised.
"""

from __future__ import annotations

from typing import Any

from nyx.critics.c006_risky_action_without_authorisation import (
    CRITIC_ID,
    CRITIC_VERSION,
    RiskyActionWithoutAuthorisationCritic,
)
from nyx.critics.context import build_context
from nyx.domain.canonical import content_hash
from nyx.domain.enums import CriticResultStatus, EvidenceKind
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem


def _event(payload: dict[str, Any]) -> Event:
    return Event(
        source_system_id="sys_actor",
        event_type="agent.action_proposed",
        occurred_at="2026-10-19T10:00:00Z",
        correlation_id="c006-corr",
        payload=payload,
        content_hash=content_hash(payload),
        trust_class="observed",
        retention_class="standard",
    )


def _approval_evidence(evidence_id: str = "ev_approval") -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        kind=EvidenceKind.REVIEW_RECORD,
        title="Change approved",
        description="The change board approved this deployment.",
        source_ref="https://reviews.example.invalid/1",
        source_system_id="sys_actor",
        # EvidenceItem requires a relevance target: evidence must say what it is evidence FOR.
        relevance_to_finding="fnd_placeholder",
        captured_at="2026-10-19T09:00:00Z",
        valid_from="2026-10-19T09:00:00Z",
        integrity_method="none",
        limitations=["Synthetic test evidence."],
        sensitivity="internal",
        access_policy_ref="test",
        retention_class="standard",
    )


def _run(payload: dict[str, Any], evidence: list[EvidenceItem] | None = None):
    context = build_context(
        target_event=_event(payload),
        linked_claim=None,
        related_claims=[],
        linked_evidence=evidence or [],
        now=None,
    )
    return RiskyActionWithoutAuthorisationCritic().run(context)


# --------------------------------------------------------------------------- positive


def test_high_impact_action_without_approval_fires() -> None:
    result = _run({"action": "deploy"})

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION
    assert "deploy" in result.proposition
    assert result.requires_human_review is True


def test_finding_states_that_nyx_took_no_action() -> None:
    """Safety invariant 6: the finding must say NYX did not act on it."""

    result = _run({"action": "delete"})

    assert result.result is CriticResultStatus.FINDING
    joined = " ".join(result.limitations).lower()
    assert "reports and records only" in joined
    assert "not performed" in joined or "has not performed" in joined
    assert "authorised policy gate" in joined


def test_finding_does_not_assert_the_action_was_unauthorised() -> None:
    """Absent telemetry is unknown, not misconduct."""

    result = _run({"action": "revoke"})

    assert result.result is CriticResultStatus.FINDING
    joined = (" ".join(result.limitations) + " " + result.rationale).lower()
    assert "not proof" in joined
    assert result.unknowns


def test_compound_action_names_are_recognised() -> None:
    """deploy_production is a deploy."""

    assert _run({"proposed_action": "deploy_production"}).result is CriticResultStatus.FINDING


# --------------------------------------------------------------------------- controls


def test_high_impact_action_with_approval_ref_stays_quiet() -> None:
    result = _run({"action": "deploy", "approval_ref": "CHG-1234"})

    assert result.result is CriticResultStatus.NO_FINDING


def test_high_impact_action_with_approval_evidence_stays_quiet() -> None:
    result = _run({"action": "deploy"}, evidence=[_approval_evidence()])

    assert result.result is CriticResultStatus.NO_FINDING


def test_ordinary_action_stays_quiet() -> None:
    """A routine operation must not be accused."""

    result = _run({"action": "read"})

    assert result.result is CriticResultStatus.NO_FINDING
    assert "not classified as high-impact" in result.proposition


def test_no_declared_action_stays_quiet() -> None:
    result = _run({"note": "nothing to see"})

    assert result.result is CriticResultStatus.NO_FINDING


def test_substring_is_not_treated_as_a_high_impact_action() -> None:
    """`undeploy` is its own verb, not `deploy` -- no substring matching."""

    result = _run({"action": "undeploy"})

    assert result.result is CriticResultStatus.NO_FINDING


def test_blank_approval_ref_does_not_count_as_approval() -> None:
    """An empty approval field is not an approval."""

    result = _run({"action": "deploy", "approval_ref": "   "})

    assert result.result is CriticResultStatus.FINDING
