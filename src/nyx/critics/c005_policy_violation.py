"""C-005 — Policy violation (SPEC §8).

Evaluates the registered policy rules against the record derived from the linked claim
and target event. Each violation names the exact policy id, the policy version that
decided, the input record it was evaluated against, and the decision. A ``no_finding``
result means only that no registered rule was violated within this critic's declared
scope; it is never a statement that the claim is true or that the code is correct.
"""

from __future__ import annotations

import json
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
from nyx.policies.engine import PolicyDecision, PolicyRegistry, default_registry

CRITIC_ID = "c005-policy-violation"
CRITIC_VERSION = "1.0.0"

_VERIFICATION_KINDS = frozenset({EvidenceKind.CI_TEST_RESULT, EvidenceKind.TEST_RESULT})


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


def _payload_from(context: CriticContext) -> dict[str, Any]:
    """Derive the policy payload from the linked claim, event, and linked evidence.

    The payload is the only thing the rules see. ``requires_approval`` is true for
    completion claims; ``approval_ref``/``approver_principal_id`` come from a linked
    review-record evidence item, if any; ``linked_verification_count`` counts linked
    verification evidence.
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
    return {
        "claim_type": claim.claim_type.value if claim is not None else None,
        "requires_approval": (claim is not None and claim.claim_type is ClaimType.COMPLETION),
        "claimant_principal_id": claim.claimant_principal_id if claim else None,
        "approver_principal_id": approver,
        "approval_ref": approval_ref,
        "linked_verification_count": sum(
            1 for item in context.linked_evidence if item.kind in _VERIFICATION_KINDS
        ),
    }


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
        ],
        cannot_assess=[
            "code correctness",
            "the truth of the linked claim's statement",
            "the security or correctness of the policy engine itself",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def __init__(self, registry: PolicyRegistry | None = None) -> None:
        self._registry = registry if registry is not None else default_registry()

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

        payload = _payload_from(context)
        evaluations = self._registry.evaluate_all(payload)
        violations = [
            evaluation
            for evaluation in evaluations
            if evaluation.decision is PolicyDecision.VIOLATED
        ]
        if not violations:
            return _no_finding(
                f"Claim {claim.id} satisfies all applicable registered policy rules.",
                f"Evaluated {len(evaluations)} registered policy rule(s) against the "
                f"record derived from claim {claim.id}; no rule was violated. This "
                "no_finding means only that no registered policy rule was violated "
                "within this critic's declared scope; it is not a statement that the "
                "claim is true or that the code is correct.",
                context.scope,
            )

        payload_text = json.dumps(payload, sort_keys=True, default=str)
        observations = [
            f"Evaluated {len(evaluations)} registered policy rule(s) against the "
            f"record derived from claim {claim.id} (input record: {payload_text})."
        ]
        for violation in violations:
            observations.append(
                f"VIOLATED policy '{violation.rule_id}' version "
                f"{violation.rule_version} evaluated against the record derived from "
                f"claim {claim.id} (input record: {payload_text}); decision="
                f"{violation.decision.value}; rationale: {violation.rationale}"
            )

        summary = "; ".join(
            f"{violation.rule_id}@{violation.rule_version} decision={violation.decision.value}"
            for violation in violations
        )
        recommendations = [
            f"Remediate {violation.rule_id}@{violation.rule_version}: "
            f"{self._registry.get(violation.rule_id, violation.rule_version).description}"
            for violation in violations
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
