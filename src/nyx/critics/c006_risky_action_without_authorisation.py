"""C-006 — risky action without authorisation (SPEC §8).

Detects a proposed or recorded *high-impact* action that carries no corresponding permission
or approval record.

**The MVP reports and records only.** This critic never performs the action, never disables or
suspends the monitored system, never revokes a credential and never changes a permission. That
restriction is safety invariant 6, and it is not a limitation of effort — it is the design.
NYX is a critic, not an enforcement mechanism: enforcement belongs to a separate, authorised
policy gate that a human owns. The finding therefore carries that boundary in its own
``limitations`` so a reader cannot mistake the finding for an action having been taken.

A ``no_finding`` result means **only** that this critic found no unauthorised high-impact action
within its declared scope. It is never a statement that the action was safe, that the actor was
trustworthy, or that no other critic could find something.
"""

from __future__ import annotations

from typing import Any

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Scope
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    EvidenceKind,
    FindingCategory,
    Severity,
)

CRITIC_ID = "c006-risky-action-without-authorisation"
CRITIC_VERSION = "1.0.0"

#: Actions that change state in a way that is expensive or hard to reverse. Matching is on a
#: normalised token, so "deploy" and "deploy_production" both match. Deliberately conservative:
#: an unrecognised verb is NOT treated as high-impact, because inventing high-impact actions
#: would manufacture findings. A missed verb is a false negative we can extend later; a
#: fabricated one is a false accusation.
_HIGH_IMPACT_ACTIONS = frozenset(
    {
        "deploy",
        "delete",
        "destroy",
        "drop",
        "revoke",
        "grant",
        "disable",
        "shutdown",
        "terminate",
        "rollback",
        "publish",
        "spend",
        "transfer",
        "rotate_credentials",
        "modify_permissions",
        "change_permissions",
    }
)

#: Payload keys a producer may use to describe the action under consideration.
_ACTION_KEYS = ("action", "proposed_action", "operation", "requested_action")

#: Payload keys that record an explicit approval.
_APPROVAL_KEYS = ("approval_ref", "approved_by", "approval_id", "authorised_by", "authorized_by")

#: Evidence kinds that count as an approval record when linked to the event.
_APPROVAL_EVIDENCE_KINDS = frozenset({EvidenceKind.REVIEW_RECORD, EvidenceKind.POLICY_EVALUATION})


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no unauthorised high-impact action within
    its declared scope. It is never a statement that the action was safe.
    """

    return CriticResult(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        result=CriticResultStatus.NO_FINDING,
        proposition=proposition,
        scope=scope,
        observations=[],
        rationale=rationale,
        recommended_next_step="No action required from this critic.",
    )


def _action_text(payload: dict[str, Any]) -> str | None:
    """Return the declared action as a normalised token, or None when none is declared."""

    for key in _ACTION_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower().replace("-", "_").replace(" ", "_")
    return None


def _is_high_impact(action: str) -> bool:
    """Return whether ``action`` names a high-impact operation.

    Matches on the whole token or on the token's leading segment, so ``deploy_production``
    counts as ``deploy``. It never matches on a substring, so ``undeploy`` and ``redeploy``
    are treated as their own verbs rather than as ``deploy``.
    """

    if action in _HIGH_IMPACT_ACTIONS:
        return True
    head = action.split("_", 1)[0]
    return head in _HIGH_IMPACT_ACTIONS


def _approval_ref(payload: dict[str, Any]) -> str | None:
    """Return the recorded approval reference from the payload, if any."""

    for key in _APPROVAL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _has_approval_evidence(context: CriticContext) -> str | None:
    """Return the id of a linked evidence item that acts as an approval record, if any."""

    for item in context.linked_evidence:
        if item.kind in _APPROVAL_EVIDENCE_KINDS:
            return item.id
    return None


class RiskyActionWithoutAuthorisationCritic:
    """Detect high-impact actions that carry no approval record."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Detect proposed or recorded high-impact actions without a corresponding permission "
        "or approval record. Reports and records only; never performs the action."
    )
    scope = CriticScope(
        can_assess=[
            "actions a producer declares in the event payload or that are recorded as evidence",
            "whether an approval or permission record exists for that action",
        ],
        cannot_assess=[
            "whether an action was actually performed in another system",
            "whether an approval record is genuine or was issued under duress",
            "actions a producer does not declare",
            "code correctness or deployment state",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def run(self, context: CriticContext) -> CriticResult:
        payload = context.target_event.payload
        action = _action_text(payload)

        if action is None:
            return _no_finding(
                "No action is declared on this event.",
                "This critic assesses declared high-impact actions; the event declares no "
                "action, so there is nothing to assess within this critic's declared scope.",
                context.scope,
            )

        if not _is_high_impact(action):
            return _no_finding(
                f"Action {action!r} is not classified as high-impact.",
                f"The declared action {action!r} is not in this critic's high-impact vocabulary, "
                "so it is out of scope. This is not a statement that the action was safe.",
                context.scope,
            )

        approval = _approval_ref(payload)
        if approval is None:
            approval = _has_approval_evidence(context)

        if approval is not None:
            return _no_finding(
                f"High-impact action {action!r} carries an approval record.",
                f"The high-impact action {action!r} names approval {approval!r}, so this critic "
                "finds nothing within its declared scope. It does not assess whether that "
                "approval was properly granted.",
                context.scope,
            )

        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"High-impact action {action!r} is recorded for system "
                f"{context.target_event.source_system_id} with no approval record."
            ),
            scope=context.scope,
            observations=[
                f"Event {context.target_event.id} declares action {action!r}.",
                "No approval reference appears in the event payload and no review_record or "
                "policy_evaluation evidence is linked to it.",
                "The action is classified as high-impact by this critic's declared vocabulary.",
            ],
            inferences=[
                "The action may have been taken without authorisation, or its authorisation may "
                "simply not have been recorded.",
            ],
            limitations=[
                "NYX REPORTS AND RECORDS ONLY. It has not performed, blocked, reversed or "
                "prevented this action, and it cannot disable or suspend the system. Any "
                "enforcement requires a separate authorised policy gate.",
                "Absence of an approval record is not proof that approval was absent: the "
                "authorisation may exist outside NYX. Absent telemetry is unknown, not "
                "misconduct.",
                "This critic does not assess whether an approval record, where present, was "
                "validly granted.",
            ],
            unknowns=[
                "Whether the action was actually executed in the target system.",
                "Whether an authorisation exists that was never recorded here.",
            ],
            rationale=(
                f"The event declares the high-impact action {action!r} and carries no approval "
                "reference, and no approval-shaped evidence is linked. The finding records the "
                "gap; it does not assert that the action was unauthorised."
            ),
            recommended_next_step=(
                "Ask the owning system for the authorisation record for this action, and link "
                "it as evidence. Route any enforcement decision to the authorised policy gate."
            ),
            requires_human_review=True,
            category=FindingCategory.SECURITY_RISK,
            severity=Severity.HIGH,
            title=f"High-impact action {action!r} with no approval record",
            summary=(
                f"System {context.target_event.source_system_id} recorded the high-impact "
                f"action {action!r} without an approval reference or approval-shaped evidence."
            ),
            event_ids=[context.target_event.id],
            affected_system_ids=[context.target_event.source_system_id],
        )
