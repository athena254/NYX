"""C-005 — Policy violation (SPEC §8).

Evaluates the registered policy rules against the record derived from the linked claim
and target event. Each violation names the exact policy id, the policy version that
decided, the input record it was evaluated against, and the decision. A ``no_finding``
result means only that no registered rule was violated within this critic's declared
scope; it is never a statement that the claim is true or that the code is correct.

The registry holds **callables**; :mod:`nyx.policies.rules` holds **declarative data**. Both are
evaluated, because the callable stays the evaluation engine and the declarative rule is the
serialisable half of the same policy model. Which one decided is visible in the finding's
observations as the rule source.

Two rules, in the order they are decided:

* RULE 1 (callable registry): every versioned callable rule in the registry is evaluated against
  the derived payload, exactly as before the declarative half of the model existed.
* RULE 2 (declarative rules): every :class:`~nyx.policies.rules.PolicyRule` registered with this
  critic is evaluated as data against the same payload, and its decision is reported with the
  same id, version, input record, and rationale shape.

**Deliberately conservative.** A declarative rule fires only on an exact, declared match
against the payload keys it names. A payload that does not carry the key a rule reads is
``NOT_APPLICABLE``, never ``VIOLATED``: "this record says nothing about the thing the rule
governs" is unknown, not non-compliance. Declarative rules are **off by default** and are only
evaluated when they are registered, because a rule set is a deployment decision and enabling it
must not silently change what the pack reports.

``CRITIC_VERSION`` was bumped from ``1.0.0`` to ``1.1.0`` to record the declarative rule path:
a finding raised from a ``PolicyRule`` is a different kind of decision from one raised by a
callable, and the version says which rule set produced it.

Limitation: the policy model has **no** concept of "an action governed by no known policy". A
rule set that does not mention an action has nothing to say about it, and the only honest outcome
is ``NOT_APPLICABLE`` — reported here as ``no_finding``, never as a violation. Which actions
*should* have a policy is a coverage question this model cannot answer, and it is listed in
``cannot_assess`` rather than papered over. This critic also sees only the payload derived from
the linked claim, event, and evidence; a policy that needs a field the payload does not carry
cannot be evaluated at all.

A ``no_finding`` result means **only** that this critic found no issue within its declared
scope. It is never a statement that the claim is true, that the code is correct, or that no
other critic could find something.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Scope
from nyx.domain.enums import (
    ClaimType,
    CriticResultStatus,
    CriticType,
    EvidenceKind,
    FindingCategory,
    Severity,
)
from nyx.domain.evidence import EvidenceItem
from nyx.policies.engine import PolicyDecision, PolicyRegistry, default_registry
from nyx.policies.rules import PolicyRule

CRITIC_ID = "c005-policy-violation"
CRITIC_VERSION = "1.1.0"

_VERIFICATION_KINDS = frozenset({EvidenceKind.CI_TEST_RESULT, EvidenceKind.TEST_RESULT})

#: Payload keys the declarative rules read. They are added to the derived payload only when a
#: declarative rule is registered, so the callable rules see exactly the payload they saw before.
_DECLARATIVE_PAYLOAD_KEYS = (
    "actions",
    "completed_steps",
    "approver_roles",
    "environment",
    "data_handling",
)


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no issue within its declared scope.
    It is never a statement that the claim is true or that the code is correct.
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


def _payload_from(context: CriticContext, *, declarative: bool = False) -> dict[str, Any]:
    """Derive the policy payload from the linked claim, event, and linked evidence.

    The payload is the only thing the rules see. ``requires_approval`` is true for
    completion claims; ``approval_ref``/``approver_principal_id`` come from a linked
    review-record evidence item, if any; ``linked_verification_count`` counts linked
    verification evidence.

    With ``declarative`` on, the keys the declarative rule kinds read are added, taken from the
    event payload (producer data) and the linked review record. They are added only when a
    declarative rule is registered, so the callable rules see exactly the payload they saw before
    the declarative half of the model existed.
    """

    claim = context.linked_claim
    review_records = [
        item for item in context.linked_evidence if item.kind is EvidenceKind.REVIEW_RECORD
    ]
    approval_ref = review_records[0].id if review_records else None
    approver = (
        review_records[0].producer_identity.id
        if review_records and review_records[0].producer_identity is not None
        else None
    )
    payload: dict[str, Any] = {
        "claim_type": claim.claim_type.value if claim is not None else None,
        "requires_approval": (claim is not None and claim.claim_type is ClaimType.COMPLETION),
        "claimant_principal_id": claim.claimant_principal_id if claim else None,
        "approver_principal_id": approver,
        "approval_ref": approval_ref,
        "linked_verification_count": sum(
            1 for item in context.linked_evidence if item.kind in _VERIFICATION_KINDS
        ),
    }
    if declarative:
        event_payload = context.target_event.payload
        payload["actions"] = event_payload.get("actions", [])
        payload["completed_steps"] = event_payload.get("completed_steps", [])
        payload["approver_roles"] = _approver_roles(review_records, event_payload)
        payload["environment"] = event_payload.get("environment")
        payload["data_handling"] = event_payload.get("data_handling", {})
    return payload


def _approver_roles(review_records: list[EvidenceItem], event_payload: dict[str, Any]) -> list[str]:
    """Return the approver roles the linked records declare, if any.

    The event payload is producer data, so its ``approver_roles`` key is read first. A review
    record carries a producer identity rather than a role list, so its principal kind is used as
    a role only when the payload declares none. Absent roles are unknown, not a violation: an
    empty list means the rule sees no approver and reports ``NOT_APPLICABLE``.
    """

    declared = event_payload.get("approver_roles")
    if isinstance(declared, list):
        return [role for role in declared if isinstance(role, str) and role]
    roles: list[str] = []
    for record in review_records:
        principal = record.producer_identity
        if principal is not None and principal.kind.value not in roles:
            roles.append(principal.kind.value)
    return roles


class PolicyViolationCritic:
    """Evaluate registered policy rules against the linked claim / target event."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Evaluate versioned, deterministic policy rules against the record derived "
        "from the linked claim and target event; report each violated rule with its "
        "policy id, version, input record, and decision."
    )
    scope = CriticScope(
        can_assess=[
            "policy-rule compliance of a linked claim against the registered rules",
            "declarative policy rules registered as data, evaluated as data",
        ],
        cannot_assess=[
            "code correctness",
            "the truth of the linked claim's statement",
            "the security or correctness of the policy engine itself",
            "whether an action is governed by *no* known policy: the policy model has no such "
            "concept, so an ungoverned action is reported as no_finding, not as a violation",
            "policies that need a payload field this critic does not derive",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def __init__(
        self,
        registry: PolicyRegistry | None = None,
        declarative_rules: Sequence[PolicyRule] | None = None,
    ) -> None:
        self._registry = registry if registry is not None else default_registry()
        self._declarative_rules = list(declarative_rules or [])

    def _declarative_description(self, rule_id: str) -> str:
        """Return the description of a declarative rule by id, for the remediation line.

        A declarative rule is data, not a registry entry, so its description is looked up in the
        list this critic was given rather than in the callable registry.
        """

        for rule in self._declarative_rules:
            if rule.rule_id == rule_id:
                return rule.description
        return "declarative policy rule"

    def run(self, context: CriticContext) -> CriticResult:
        claim = context.linked_claim
        if claim is None:
            return _no_finding(
                "No claim is linked to this event.",
                "This critic assesses policy-rule compliance of a linked claim; without a "
                "linked claim there is no record to evaluate within this critic's "
                "declared scope.",
                context.scope,
            )

        payload = _payload_from(context, declarative=bool(self._declarative_rules))
        evaluations = self._registry.evaluate_all(payload)
        sources = ["callable"] * len(evaluations)
        for rule in self._declarative_rules:
            evaluations.append(rule.evaluate(payload))
            sources.append("declarative")
        violations = [
            (evaluation, source)
            for evaluation, source in zip(evaluations, sources, strict=True)
            if evaluation.decision is PolicyDecision.VIOLATED
        ]
        if not violations:
            return _no_finding(
                f"Claim {claim.id} satisfies all applicable registered policy rules.",
                f"Evaluated {len(evaluations)} registered policy rule(s) against the "
                f"record derived from claim {claim.id}; no rule was violated. This "
                "no_finding means only that no registered policy rule was violated "
                "within this critic's declared scope; it is not a statement that the "
                "claim is true or that the code is correct. An action governed by no "
                "known policy is not a violation: the policy model has no such concept.",
                context.scope,
            )

        payload_text = json.dumps(payload, sort_keys=True, default=str)
        observations = [
            f"Evaluated {len(evaluations)} registered policy rule(s) against the "
            f"record derived from claim {claim.id} (input record: {payload_text})."
        ]
        for violation, source in violations:
            observations.append(
                f"VIOLATED policy '{violation.rule_id}' version "
                f"{violation.rule_version} ({source} rule) evaluated against the record "
                f"derived from claim {claim.id} (input record: {payload_text}); decision="
                f"{violation.decision.value}; rationale: {violation.rationale}"
            )

        summary = "; ".join(
            f"{violation.rule_id}@{violation.rule_version} decision={violation.decision.value}"
            for violation, _ in violations
        )
        recommendations = [
            f"Remediate {violation.rule_id}@{violation.rule_version}: "
            f"{self._registry.get(violation.rule_id, violation.rule_version).description}"
            if source == "callable"
            else f"Remediate {violation.rule_id}@{violation.rule_version}: "
            f"{self._declarative_description(violation.rule_id)}"
            for violation, source in violations
        ]

        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"Record derived from claim {claim.id} violates "
                f"{len(violations)} registered policy rule(s)."
            ),
            scope=claim.scope,
            observations=observations,
            inferences=[
                "The record derived from the claim does not satisfy the registered "
                "policy; the observations name each violated rule, the version that "
                "decided, the input record, and the decision.",
            ],
            limitations=[
                "This finding assesses policy-rule compliance only; it does not judge "
                "code correctness or the truth of the claim statement.",
                "Rules see only the payload derived by this critic from the linked "
                "claim, target event, and linked evidence.",
            ],
            rationale=(
                f"Record derived from claim {claim.id} violates {len(violations)} "
                f"registered policy rule(s): {summary}. Each violation names the policy "
                "id, the policy version that decided, the input record it was "
                "evaluated against, and the decision."
            ),
            recommended_next_step=(
                "Route the named policy violations to an authorized decision-maker "
                "for review and remediation."
            ),
            category=FindingCategory.POLICY_VIOLATION,
            severity=Severity.HIGH,
            title=(f"Policy violation: {len(violations)} rule(s) violated by claim {claim.id}"),
            summary=(
                f"Record derived from claim {claim.id} violates {len(violations)} "
                f"registered policy rule(s): {summary}."
            ),
            recommendations=recommendations,
            claim_ids=[claim.id],
            event_ids=[context.target_event.id],
            affected_system_ids=[claim.claimant_system_id],
            requires_human_review=True,
        )
