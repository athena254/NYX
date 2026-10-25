"""C-006 — risky action without authorisation (SPEC §8).

Detects a proposed or recorded *high-impact* action that carries no corresponding permission
or approval record, and — when the stronger rule is enabled — an approval record that does
not actually authorise the action it is cited for.

**The MVP reports and records only.** This critic never performs the action, never disables or
suspends the monitored system, never revokes a credential and never changes a permission. That
restriction is safety invariant 6, and it is not a limitation of effort — it is the design.
NYX is a critic, not an enforcement mechanism: enforcement belongs to a separate, authorised
policy gate that a human owns. The finding therefore carries that boundary in its own
``limitations`` so a reader cannot mistake the finding for an action having been taken.

Two rules:

* RULE 1 (unauthorised action): the event declares a high-impact action and names no approval
  reference, and no approval-shaped evidence is linked. This is the 1.0.0 behaviour, unchanged.
* RULE 2 (invalid authorisation): an approval reference is present, but the authorisation
  record it points at does not authorise *this* action — the approver is the actor, the
  recorded scope does not cover the action's scope, or the authorisation has expired. Gated by
  ``require_valid_authorisation``.

RULE 2 exists because a reference is not an authorisation. A payload field named
``approval_ref`` proves only that a string was present, and the three ways that string gives
false comfort are exactly the three cases it cannot express: self-approval, a scope that does
not cover the action, and an expiry that has passed. Those are the findings RULE 2 adds.

The risk taxonomy (``RiskAction``) classifies what was detected; the token set below stays the
*detection* mechanism and is deliberately conservative — an unrecognised verb is NOT treated as
high-impact, because inventing high-impact actions would manufacture findings. A detected action
that maps to no category is still high-impact and reports an unclassified category rather than
being dropped.

A ``no_finding`` result means **only** that this critic found no unauthorised high-impact action
within its declared scope. It is never a statement that the action was safe, that the actor was
trustworthy, or that no other critic could find something.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.authorisation import Authorisation
from nyx.domain.base import Digest, Scope
from nyx.domain.canonical import content_hash
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    EvidenceKind,
    FindingCategory,
    RiskAction,
    Severity,
    VerificationStrength,
)

CRITIC_ID = "c006-risky-action-without-authorisation"
CRITIC_VERSION = "1.1.0"

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

#: The taxonomy each detected token maps into. A token absent here is still high-impact — it is
#: reported with an ``unclassified`` category rather than dropped, because the detection set is
#: the conservative half and the classification is a label, not a gate.
_RISK_CATEGORY: dict[str, RiskAction] = {
    "deploy": RiskAction.DEPLOY,
    "rollback": RiskAction.DEPLOY,
    "publish": RiskAction.DEPLOY,
    "delete": RiskAction.DELETE,
    "destroy": RiskAction.DELETE,
    "drop": RiskAction.DELETE,
    "disable": RiskAction.OVERWRITE_PRODUCTION_STATE,
    "shutdown": RiskAction.OVERWRITE_PRODUCTION_STATE,
    "terminate": RiskAction.OVERWRITE_PRODUCTION_STATE,
    "revoke": RiskAction.PERMISSION_CHANGE,
    "grant": RiskAction.PERMISSION_CHANGE,
    "modify_permissions": RiskAction.PERMISSION_CHANGE,
    "change_permissions": RiskAction.PERMISSION_CHANGE,
    "spend": RiskAction.SPEND,
    "transfer": RiskAction.SPEND,
    "rotate_credentials": RiskAction.SECRET_ACCESS,
}

#: The category reported when a detected action has no taxonomy entry. Never a silent pass.
UNCLASSIFIED_RISK = "unclassified"

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


def _risk_category(action: str) -> str:
    """Return the taxonomy label for a detected action, or ``unclassified``.

    The head segment is tried first so ``deploy_production`` classifies as ``deploy``; a token
    that matches nothing is labelled ``unclassified`` rather than dropped, because the detection
    is the conservative half and a missing label must never read as a clean action.
    """

    head = action.split("_", 1)[0]
    for candidate in (action, head):
        category = _RISK_CATEGORY.get(candidate)
        if category is not None:
            return category.value
    return UNCLASSIFIED_RISK


def _root_cause_key(*, action: str, reference: str) -> Digest:
    """A stable defect identity for one unauthorised action, so repeats dedupe to one finding."""

    return content_hash({"critic_id": CRITIC_ID, "action": action, "reference": reference})


class RiskyActionWithoutAuthorisationCritic:
    """Detect high-impact actions that carry no approval record, or an invalid one."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Detect proposed or recorded high-impact actions without a corresponding permission "
        "or approval record; when enabled, also detect an approval record that does not "
        "authorise the action it is cited for. Reports and records only; never performs the "
        "action."
    )
    scope = CriticScope(
        can_assess=[
            "actions a producer declares in the event payload or that are recorded as evidence",
            "whether an approval or permission record exists for that action",
            "when an authorisation record is resolvable: whether the approver differs from the "
            "actor, whether its scope covers the action, and whether it has expired",
        ],
        cannot_assess=[
            "whether an action was actually performed in another system",
            "whether an approval record is genuine or was issued under duress",
            "actions a producer does not declare",
            "code correctness or deployment state",
            "an authorisation record that is not resolvable through the injected lookup: an "
            "absent lookup is unknown, never an approval and never a refusal",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def __init__(
        self,
        *,
        require_valid_authorisation: bool = False,
        authorisation_lookup: Callable[[str], Authorisation | None] | None = None,
    ) -> None:
        """Construct the critic.

        ``require_valid_authorisation`` switches RULE 2 on. It is a constructor argument rather
        than a module constant so a deployment can adopt the stronger rule without a code
        change, and so the off-state is testable without monkeypatching. With it off this
        critic behaves exactly as it did before the ground-truth layer: a present approval
        reference still satisfies it.

        ``authorisation_lookup`` resolves an approval reference to the stored record. It is
        injected rather than opened from a session because this critic is otherwise a pure
        function over its context, and a critic that opens its own connection is a critic that
        can read -- and one day write -- state it was only meant to assess. The lookup's result
        is validated here, never trusted: a lookup that returns "the most recent record" is
        still checked for approver, scope and expiry.
        """

        self.require_valid_authorisation = require_valid_authorisation
        self._authorisation_lookup = authorisation_lookup

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
            if not self.require_valid_authorisation:
                return _no_finding(
                    f"High-impact action {action!r} carries an approval record.",
                    f"The high-impact action {action!r} names approval {approval!r}, so this "
                    "critic finds nothing within its declared scope. It does not assess whether "
                    "that approval was properly granted.",
                    context.scope,
                )
            invalid = self._invalid_authorisation(context, action=action, reference=approval)
            if invalid is not None:
                return invalid
            return _no_finding(
                f"High-impact action {action!r} carries a valid authorisation record.",
                f"The high-impact action {action!r} names approval {approval!r}, which "
                "resolves to a record whose approver differs from the actor, whose scope "
                "covers the action, and which has not expired. This critic found nothing within "
                "its declared scope; it does not assess whether the approval was genuine.",
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
                f"The action maps to risk category {_risk_category(action)!r}.",
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
            root_cause_key=_root_cause_key(action=action, reference=""),
            subject_version=context.scope.commit,
            verification_strength=VerificationStrength.SELF_REPORTED,
            remediation_hint=(
                "Record the authorisation for this action, or have the owning system confirm "
                "that none was required."
            ),
            event_ids=[context.target_event.id],
            affected_system_ids=[context.target_event.source_system_id],
        )

    def _invalid_authorisation(
        self, context: CriticContext, *, action: str, reference: str
    ) -> CriticResult | None:
        """Return a finding when the cited authorisation does not authorise this action.

        The record is resolved through the injected lookup and every field of what comes back is
        then checked here. An unresolvable reference is an unknown, not an approval and not a
        refusal, so it yields ``no_finding`` -- the honest answer is that NYX cannot tell.
        """

        if self._authorisation_lookup is None:
            return _no_finding(
                f"High-impact action {action!r} names approval {reference!r}, which NYX cannot "
                "resolve.",
                "The stronger rule is enabled but no authorisation lookup was supplied, so the "
                "record behind the reference cannot be read. This is an unknown rather than a "
                "clean approval: NYX is not asserting the action was authorised, only that it "
                "cannot assess it.",
                context.scope,
            )

        record = self._authorisation_lookup(reference)
        if record is None:
            return _no_finding(
                f"High-impact action {action!r} names approval {reference!r}, which does not "
                "resolve to a stored authorisation record.",
                "The reference points at no record NYX holds. Absent telemetry is unknown, so "
                "this critic abstains rather than treating an unresolvable reference as either "
                "an approval or a refusal.",
                context.scope,
            )

        actor = context.target_event.actor
        failures: list[str] = []
        if actor is not None and record.approver.id == actor.id:
            failures.append(
                f"the approver ({record.approver.id}) is the actor ({actor.id}): a record that "
                "approves its own action is not an authorisation"
            )
        if not record.scope_matches(context.scope.artifact or context.scope.system_id):
            failures.append(
                f"the authorisation's scope ({record.scope_ref!r}) does not cover the action's "
                f"scope ({context.scope.artifact or context.scope.system_id!r})"
            )
        if record.is_expired(context.now):
            failures.append(
                f"the authorisation expired at {record.expires_at} before the assessment time "
                f"({context.now})"
            )

        if not failures:
            return None

        detail = "; ".join(failures)
        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"High-impact action {action!r} cites authorisation {reference!r}, which does "
                f"not authorise it: {detail}."
            ),
            scope=context.scope,
            observations=[
                f"Event {context.target_event.id} declares action {action!r} and names approval "
                f"{reference!r}.",
                f"Authorisation {record.id} resolves to approver {record.approver.id}, scope "
                f"{record.scope_ref!r}, approved at {record.approved_at}, "
                f"expiring at {record.expires_at or 'never'}.",
                f"The action maps to risk category {_risk_category(action)!r}.",
            ]
            + [
                # The reason the record fails is an observation, not only prose: a reader must be
                # able to see which check failed without parsing the proposition, and the
                # observations are the part of a finding that is quoted as evidence.
                f"FAILED CHECK: {failure}"
                for failure in failures
            ],
            inferences=[
                "The action may have been taken without a valid authorisation, or the "
                "authorisation may have been recorded for a different action or a different "
                "time.",
            ],
            limitations=[
                "NYX REPORTS AND RECORDS ONLY. It has not performed, blocked, reversed or "
                "prevented this action, and it cannot disable or suspend the system. Any "
                "enforcement requires a separate authorised policy gate.",
                "A scope or expiry mismatch is a property of the recorded authorisation, not "
                "proof that the action was unauthorised: a broader record may exist outside "
                "NYX.",
                "This critic does not assess whether the authorisation was genuine or was "
                "issued under duress.",
            ],
            unknowns=[
                "Whether an authorisation covering this action exists that was never recorded.",
                "Whether the action was actually executed in the target system.",
            ],
            rationale=(
                f"The event cites authorisation {reference!r} for the high-impact action "
                f"{action!r}, and the record behind it fails this critic's checks: {detail}. "
                "A reference is not an authorisation, so the finding records the mismatch "
                "rather than accepting the reference."
            ),
            recommended_next_step=(
                "Record an authorisation whose approver differs from the actor, whose scope "
                "covers the action, and which has not expired. Route any enforcement decision "
                "to the authorised policy gate."
            ),
            requires_human_review=True,
            category=FindingCategory.SECURITY_RISK,
            severity=Severity.HIGH,
            title=f"High-impact action {action!r} with an invalid authorisation record",
            summary=(
                f"Action {action!r} cites authorisation {reference!r}, which does not authorise "
                f"it: {detail}."
            ),
            root_cause_key=_root_cause_key(action=action, reference=reference),
            subject_version=context.scope.commit,
            verification_strength=VerificationStrength.SELF_REPORTED,
            remediation_hint=(
                "Re-issue the authorisation with an approver other than the actor, a scope "
                "that covers the action, and an expiry that has not passed."
            ),
            event_ids=[context.target_event.id],
            affected_system_ids=[context.target_event.source_system_id],
        )
