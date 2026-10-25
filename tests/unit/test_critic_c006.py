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
from nyx.domain.authorisation import Authorisation
from nyx.domain.base import PrincipalRef
from nyx.domain.canonical import content_hash
from nyx.domain.enums import (
    CriticResultStatus,
    Environment,
    EvidenceKind,
    PrincipalKind,
    RiskAction,
)
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


# ---------------------------------------------------------------------------
# RULE 2: an approval reference is not an authorisation.
# ---------------------------------------------------------------------------


def _event_with_actor(payload: dict[str, Any], actor_id: str = "principal_operator") -> Event:
    event = _event(payload)
    event.actor = PrincipalRef(id=actor_id, kind=PrincipalKind.HUMAN)
    return event


def _authorisation(
    *,
    record_id: str = "aut_1",
    approver_id: str = "principal_approver",
    actor_id: str = "svc_deployer",
    scope_ref: str = "demo",
    expires_at: str | None = None,
) -> Authorisation:
    """A stored authorisation record, with a legitimate approver distinct from its actor."""

    return Authorisation(
        id=record_id,
        action="action.deploy",
        risk_action=RiskAction.DEPLOY,
        actor=PrincipalRef(id=actor_id, kind=PrincipalKind.SERVICE),
        approver=PrincipalRef(id=approver_id, kind=PrincipalKind.HUMAN),
        environment=Environment.STAGING,
        scope_ref=scope_ref,
        approved_at="2026-10-19T09:00:00Z",
        expires_at=expires_at,
    )


def _run_with_authorisation(
    payload: dict[str, Any],
    records: dict[str, Authorisation],
    *,
    require: bool = True,
):
    context = build_context(
        target_event=_event_with_actor(payload),
        linked_claim=None,
        related_claims=[],
        linked_evidence=[],
        now=None,
    )
    critic = RiskyActionWithoutAuthorisationCritic(
        require_valid_authorisation=require,
        authorisation_lookup=records.get,
    )
    return critic.run(context)


def test_valid_authorisation_passes_the_stronger_rule() -> None:
    result = _run_with_authorisation(
        {"action": "deploy", "artifact": "demo", "approval_ref": "aut_1"},
        {"aut_1": _authorisation()},
    )

    assert result.result is CriticResultStatus.NO_FINDING


def test_expired_authorisation_is_a_finding() -> None:
    """A record that lapsed before the assessment time does not authorise the action."""

    result = _run_with_authorisation(
        {"action": "deploy", "artifact": "demo", "approval_ref": "aut_1"},
        {
            "aut_1": _authorisation(
                expires_at="2026-10-19T09:30:00Z",
            )
        },
    )

    assert result.result is CriticResultStatus.FINDING
    output_text = " ".join(result.observations).lower()
    assert "expired" in output_text


def test_self_approval_is_a_finding() -> None:
    """The approver being the actor is not an authorisation, even though the record is valid.

    The stored record is well-formed -- its own ``actor`` field names the service that was
    authorised. The finding is that the *approver* is the same principal who performed the
    action, which is exactly the case a present ``approval_ref`` cannot express.
    """

    result = _run_with_authorisation(
        {"action": "deploy", "artifact": "demo", "approval_ref": "aut_1"},
        {
            "aut_1": _authorisation(
                approver_id="principal_operator",
                actor_id="svc_deployer",
            )
        },
    )

    assert result.result is CriticResultStatus.FINDING
    output_text = " ".join(result.observations).lower()
    assert "approver" in output_text


def test_scope_mismatch_is_a_finding() -> None:
    """An approval scoped to another artifact does not cover this action."""

    result = _run_with_authorisation(
        {"action": "deploy", "artifact": "demo", "approval_ref": "aut_1"},
        {"aut_1": _authorisation(scope_ref="other-service")},
    )

    assert result.result is CriticResultStatus.FINDING
    output_text = " ".join(result.observations).lower()
    assert "scope" in output_text


def test_unresolvable_reference_abstains_rather_than_passing() -> None:
    """A reference to a record NYX does not hold is unknown, not an approval or a refusal."""

    result = _run_with_authorisation(
        {"action": "deploy", "artifact": "demo", "approval_ref": "aut_missing"},
        {},
    )

    assert result.result is CriticResultStatus.NO_FINDING
    assert "cannot" in result.rationale.lower() or "unknown" in result.rationale.lower()


def test_no_lookup_supplied_abstains() -> None:
    context = build_context(
        target_event=_event_with_actor(
            {"action": "deploy", "artifact": "demo", "approval_ref": "aut_1"}
        ),
        linked_claim=None,
        related_claims=[],
        linked_evidence=[],
        now=None,
    )
    critic = RiskyActionWithoutAuthorisationCritic(require_valid_authorisation=True)

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_flag_off_still_accepts_a_present_reference() -> None:
    """Regression guard: the 1.0.0 behaviour is unchanged when RULE 2 is off."""

    result = _run_with_authorisation(
        {"action": "deploy", "artifact": "demo", "approval_ref": "aut_expired"},
        {"aut_expired": _authorisation(expires_at="2026-01-01T00:00:00Z")},
        require=False,
    )

    assert result.result is CriticResultStatus.NO_FINDING


def test_no_approval_reference_still_fires_with_the_flag_on() -> None:
    """RULE 2 does not weaken RULE 1: an absent reference is still a finding."""

    result = _run_with_authorisation(
        {"action": "deploy", "artifact": "demo"}, {"aut_1": _authorisation()}
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.root_cause_key is not None


def test_risk_category_is_reported() -> None:
    result = _run_with_authorisation({"action": "delete", "artifact": "demo"}, {})

    assert result.result is CriticResultStatus.FINDING
    observations_text = " ".join(result.observations)
    assert "delete" in observations_text
