"""Declarative policy rules beside the callable registry (SPEC §8, C-005).

:mod:`nyx.policies.engine` evaluates *callables*: a ``Rule`` is three Python functions over a
payload dict, and its ``digest`` hashes their code fingerprints. That makes a decision
reproducible and tied to an exact revision, but it makes a policy a program rather than data —
it cannot be written in a file, reviewed as a record, or shipped without a deploy.

This module is the other half, and it is deliberately **additive**: the callable stays the
evaluation engine. A :class:`PolicyRule` is typed, serialisable *data* describing one rule, and
:meth:`PolicyRule.evaluate` turns it into the same :class:`~nyx.policies.engine.RuleEvaluation`
the callable path produces, so C-005 can evaluate either without knowing which it is holding.

The five kinds are the ones the policy model names:

* ``FORBIDDEN_ACTION`` — an action token that must not appear in the payload's action list.
* ``REQUIRED_STEP`` — a step that must be present in the payload's completed-step list.
* ``REQUIRED_APPROVER`` — a role that must be among the approver roles recorded for the action.
* ``ALLOWED_ENVIRONMENT`` — the environments the action is permitted to run in.
* ``DATA_HANDLING`` — per-attribute limits on the data the action may touch.

**Deliberately conservative, in the same sense as the rest of the pack.** A rule fires only on
an exact, declared match against the payload keys it names. An unrecognised action token, a
payload that does not carry the key the rule reads, or a limit expressed in a unit the rule does
not state is **not** a violation: inventing one would manufacture findings, and a false
accusation is worse than a miss. In particular a rule that applies to no key in the payload is
``NOT_APPLICABLE``, never ``VIOLATED`` — "this payload says nothing about the thing the rule
governs" is unknown, not non-compliance.

Limitation: this model has **no** concept of "an action governed by no known policy". A rule
set that does not mention an action has nothing to say about it, and the only honest outcome is
``NOT_APPLICABLE``. C-005 therefore reports no finding for such an action, and says so in its
``cannot_assess`` and its module docstring. Coverage of the action vocabulary — which actions
*should* have a policy — is a separate question this model cannot answer.

A rule that is not ``enabled`` is skipped entirely rather than evaluated and ignored: an
inactive rule must not be able to influence a decision or a digest.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from nyx.domain.base import Identifier, NyxModel
from nyx.policies.engine import PolicyDecision, RuleEvaluation


class PolicyRuleKind(StrEnum):
    """The five declarative rule kinds the policy model names.

    Each kind is a *shape* of constraint, not a severity: what the payload must look like for
    the rule to be satisfied. A kind with no matching payload key is ``NOT_APPLICABLE``, never
    a violation.
    """

    FORBIDDEN_ACTION = "forbidden_action"
    REQUIRED_STEP = "required_step"
    REQUIRED_APPROVER = "required_approver"
    ALLOWED_ENVIRONMENT = "allowed_environment"
    DATA_HANDLING = "data_handling"


#: The payload keys each kind reads. A rule that names a key the payload does not carry is
#: ``NOT_APPLICABLE`` for that key rather than ``VIOLATED``.
_KIND_PAYLOAD_KEYS: dict[PolicyRuleKind, tuple[str, ...]] = {
    PolicyRuleKind.FORBIDDEN_ACTION: ("actions",),
    PolicyRuleKind.REQUIRED_STEP: ("completed_steps",),
    PolicyRuleKind.REQUIRED_APPROVER: ("approver_roles",),
    PolicyRuleKind.ALLOWED_ENVIRONMENT: ("environment",),
    PolicyRuleKind.DATA_HANDLING: ("data_handling",),
}


class PolicyRule(NyxModel):
    """One declarative, typed policy rule.

    Every field is data, so a rule can be written as JSON, reviewed as a record, and hashed into
    a policy version. The per-kind field lists are optional and independent: a rule carries the
    list its kind needs and leaves the others empty. ``matches`` and ``evaluate`` are the
    evaluation helpers that turn the data into a :class:`RuleEvaluation`.
    """

    rule_id: Identifier
    policy_version: str = Field(min_length=1, max_length=64)
    kind: PolicyRuleKind
    description: str = Field(min_length=1, max_length=1000)
    enabled: bool = True

    forbidden_actions: list[str] = Field(default_factory=list, max_length=64)
    required_steps: list[str] = Field(default_factory=list, max_length=64)
    required_approver_roles: list[str] = Field(default_factory=list, max_length=64)
    allowed_environments: list[str] = Field(default_factory=list, max_length=64)
    data_handling_limits: dict[str, str] = Field(default_factory=dict, max_length=32)

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical dict form of the rule.

        Deterministic key order is the caller's business (``canonical_json`` sorts), so this is
        a plain dump. It is the shape a policy version record stores, and the shape a registry
        can compare two versions of a rule with.
        """

        return self.model_dump(mode="json", by_alias=True, exclude={"rule_id"})

    def matches(self, payload: dict[str, Any]) -> bool:
        """Return whether this rule has anything to say about ``payload``.

        A rule matches only when the payload carries at least one key the rule's kind reads and
        the rule actually constrains that key. A rule that names nothing, or a payload that
        carries none of the keys, does not match — and a non-matching rule is
        ``NOT_APPLICABLE``, never a violation.
        """

        if not self.enabled:
            return False
        if self.kind is PolicyRuleKind.DATA_HANDLING:
            return bool(self.data_handling_limits) and "data_handling" in payload
        if not self._kind_values():
            return False
        return any(key in payload for key in _KIND_PAYLOAD_KEYS[self.kind])

    def evaluate(self, payload: dict[str, Any]) -> RuleEvaluation:
        """Evaluate this rule against ``payload`` and return the recorded decision.

        The three outcomes are the same as the callable engine's: ``NOT_APPLICABLE`` when the
        rule does not match, ``VIOLATED`` when a declared constraint fails, ``SATISFIED``
        otherwise. The rationale names the constraint that decided, so a reader can see which
        field of the rule produced the verdict.
        """

        if not self.matches(payload):
            return self._evaluation(
                PolicyDecision.NOT_APPLICABLE, "Rule does not apply to this payload."
            )
        breach = self._breach(payload)
        if breach is not None:
            return self._evaluation(PolicyDecision.VIOLATED, breach)
        return self._evaluation(
            PolicyDecision.SATISFIED, "Payload satisfies every constraint of this rule."
        )

    # -- the per-kind checks ------------------------------------------------

    def _kind_values(self) -> list[str]:
        """Return the non-empty constraint list for this rule's kind."""

        if self.kind is PolicyRuleKind.FORBIDDEN_ACTION:
            return self.forbidden_actions
        if self.kind is PolicyRuleKind.REQUIRED_STEP:
            return self.required_steps
        if self.kind is PolicyRuleKind.REQUIRED_APPROVER:
            return self.required_approver_roles
        if self.kind is PolicyRuleKind.ALLOWED_ENVIRONMENT:
            return self.allowed_environments
        return []

    def _breach(self, payload: dict[str, Any]) -> str | None:
        """Return the text of the first constraint this payload breaches, or ``None``."""

        if self.kind is PolicyRuleKind.FORBIDDEN_ACTION:
            actions = _string_list(payload.get("actions"))
            hits = [action for action in actions if action in self.forbidden_actions]
            if hits:
                return f"Payload records forbidden action(s): {', '.join(sorted(set(hits)))}."
            return None

        if self.kind is PolicyRuleKind.REQUIRED_STEP:
            completed = _string_list(payload.get("completed_steps"))
            missing = [step for step in self.required_steps if step not in completed]
            if missing:
                return f"Payload does not record required step(s): {', '.join(missing)}."
            return None

        if self.kind is PolicyRuleKind.REQUIRED_APPROVER:
            roles = _string_list(payload.get("approver_roles"))
            if not any(role in roles for role in self.required_approver_roles):
                return (
                    "Payload records no approver role from the required set: "
                    f"{', '.join(self.required_approver_roles)}."
                )
            return None

        if self.kind is PolicyRuleKind.ALLOWED_ENVIRONMENT:
            environment = payload.get("environment")
            if isinstance(environment, str) and environment not in self.allowed_environments:
                return (
                    f"Payload environment {environment!r} is not in the allowed set: "
                    f"{', '.join(self.allowed_environments)}."
                )
            return None

        if self.kind is PolicyRuleKind.DATA_HANDLING:
            declared = payload.get("data_handling")
            if not isinstance(declared, dict):
                return None
            for attribute, limit in self.data_handling_limits.items():
                if attribute not in declared:
                    continue
                actual = declared[attribute]
                if _limit_exceeded(limit, actual):
                    return (
                        f"Data-handling limit for {attribute!r} is {limit!r} but the payload "
                        f"records {actual!r}."
                    )
            return None

        # A kind with no branch is a gap in this model, not compliance. Raising keeps it visible:
        # the alternative is a silent ``None`` that reads as "satisfied" for a constraint nobody
        # wrote a check for.
        raise NotImplementedError(f"no breach check for policy rule kind {self.kind.value!r}")

    def _evaluation(self, decision: PolicyDecision, rationale: str) -> RuleEvaluation:
        return RuleEvaluation(
            rule_id=self.rule_id,
            rule_version=self.policy_version,
            decision=decision,
            rationale=rationale,
        )


def _string_list(value: Any) -> list[str]:
    """Return ``value`` as a list of non-empty strings, tolerating a single bare string.

    A payload is producer data, so a single value is read as a one-element list rather than
    rejected: refusing it would turn a shape difference into a silent non-match.
    """

    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _limit_exceeded(limit: str, actual: Any) -> bool:
    """Return whether ``actual`` exceeds a declared limit.

    The limit is data, so it is read as a number when it can be one and the payload value is
    numeric too. A limit in a unit this function cannot parse is **not** exceeded: an
    unrecognised token is not a violation, and guessing a unit would manufacture findings.
    """

    if not isinstance(actual, (int, float)):
        return False
    if isinstance(actual, bool):
        return False
    try:
        threshold = float(limit)
    except (TypeError, ValueError):
        return False
    return float(actual) > threshold
