"""NYX policy engine (SPEC §8, C-005).

A minimal, deterministic policy registry. Rules are pure functions over a payload dict,
so a decision is reproducible and can be tied to an exact rule revision via
:meth:`PolicyRegistry.digest`. ``no_finding`` consumers (the C-005 critic) treat a
``NOT_APPLICABLE`` or ``SATISFIED`` decision as "nothing to report"; only ``VIOLATED``
decisions become findings.

Versioning: multiple versions of one rule id coexist. ``get`` with ``version=None``
resolves the latest version; an explicit version resolves exactly that version.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from nyx.domain.canonical import content_hash


class PolicyDecision(StrEnum):
    """The outcome of evaluating one rule against one payload."""

    SATISFIED = "satisfied"
    VIOLATED = "violated"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class Rule:
    """One versioned policy rule.

    ``applies_to`` decides whether the rule has anything to say about the payload at all;
    ``violated`` decides the breach; ``rationale`` renders the decision as text. All
    three are pure functions of the payload, so the same payload always yields the same
    decision.
    """

    rule_id: str
    version: str
    description: str
    applies_to: Callable[[dict[str, Any]], bool]
    violated: Callable[[dict[str, Any]], bool]
    rationale: Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class RuleEvaluation:
    """The recorded outcome of one rule against one payload."""

    rule_id: str
    rule_version: str
    decision: PolicyDecision
    rationale: str


class UnknownPolicyError(LookupError):
    """Raised when a rule id, or a version of a known id, is not registered."""


def _version_key(version: str) -> tuple[tuple[int, int | str], ...]:
    """Return a comparable key for a ``major.minor.patch``-style version string.

    Numeric segments compare numerically; non-numeric segments (pre-release tags)
    compare lexically after all numeric segments.
    """

    key: list[tuple[int, int | str]] = []
    for part in version.split("."):
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def _callable_fingerprint(fn: Callable[..., Any]) -> str:
    """Return a stable identity for a callable's code object."""

    code = fn.__code__
    return f"{fn.__module__}.{fn.__qualname__}:{code.co_code.hex()}"


class PolicyRegistry:
    """An in-memory registry of versioned rules.

    Adding a rule with a new (id, version) pair appends it; re-adding the same
    (id, version) replaces the previous entry. Older versions are never removed.
    """

    def __init__(self) -> None:
        self._rules: dict[tuple[str, str], Rule] = {}

    def add(self, rule: Rule) -> None:
        """Register a rule, replacing any existing rule with the same (id, version)."""

        self._rules[(rule.rule_id, rule.version)] = rule

    def get(self, rule_id: str, version: str | None = None) -> Rule:
        """Resolve a rule by id, optionally pinning an exact version.

        ``version=None`` resolves the latest registered version of the id.
        """

        if version is not None:
            try:
                return self._rules[(rule_id, version)]
            except KeyError:
                raise UnknownPolicyError(
                    f"Unknown version {version!r} of policy rule id {rule_id!r}."
                ) from None
        candidates = [rule for (rid, _), rule in self._rules.items() if rid == rule_id]
        if not candidates:
            raise UnknownPolicyError(f"Unknown policy rule id {rule_id!r}.")
        return max(candidates, key=lambda rule: _version_key(rule.version))

    def list_rules(self) -> list[Rule]:
        """Return every registered rule, in registration order."""

        return list(self._rules.values())

    def digest(self, rule_id: str, version: str | None = None) -> str:
        """Return a stable content hash tying a decision to an exact rule revision."""

        rule = self.get(rule_id, version)
        material = {
            "rule_id": rule.rule_id,
            "version": rule.version,
            "description": rule.description,
            "applies_to": _callable_fingerprint(rule.applies_to),
            "violated": _callable_fingerprint(rule.violated),
            "rationale": _callable_fingerprint(rule.rationale),
        }
        return content_hash(material)

    def evaluate(
        self, rule_id: str, payload: dict[str, Any], version: str | None = None
    ) -> RuleEvaluation:
        """Evaluate one rule against one payload.

        ``NOT_APPLICABLE`` when the rule does not apply to the payload; otherwise
        ``VIOLATED`` or ``SATISFIED``. The returned ``rule_version`` is the version
        that actually decided.
        """

        rule = self.get(rule_id, version)
        if not rule.applies_to(payload):
            decision = PolicyDecision.NOT_APPLICABLE
        elif rule.violated(payload):
            decision = PolicyDecision.VIOLATED
        else:
            decision = PolicyDecision.SATISFIED
        return RuleEvaluation(
            rule_id=rule.rule_id,
            rule_version=rule.version,
            decision=decision,
            rationale=rule.rationale(payload),
        )

    def evaluate_all(self, payload: dict[str, Any]) -> list[RuleEvaluation]:
        """Evaluate the latest version of every registered rule id against a payload."""

        latest_ids: list[str] = []
        for rule_id in dict.fromkeys(rid for rid, _ in self._rules):
            latest_ids.append(rule_id)
        return [self.evaluate(rule_id, payload) for rule_id in latest_ids]


def _approval_required_recorded() -> Rule:
    """A record that claims a high-impact completion must carry an approval reference."""

    return Rule(
        rule_id="approval.required-recorded",
        version="1.0.0",
        description=(
            "A record that claims a high-impact completion must carry an approval reference."
        ),
        applies_to=lambda payload: bool(payload.get("requires_approval")),
        violated=lambda payload: not payload.get("approval_ref"),
        rationale=lambda payload: (
            "Record requires approval (requires_approval is true) but no approval_ref is recorded."
        ),
    )


def _approval_no_self_approval() -> Rule:
    """The approver must not be the claimant."""

    return Rule(
        rule_id="approval.no-self-approval",
        version="1.0.0",
        description="The approver must not be the claimant.",
        applies_to=lambda payload: (
            payload.get("claimant_principal_id") is not None
            and payload.get("approver_principal_id") is not None
        ),
        violated=lambda payload: (
            payload["claimant_principal_id"] == payload["approver_principal_id"]
        ),
        rationale=lambda payload: (
            f"Approver {payload['approver_principal_id']!r} is the same principal as "
            f"claimant {payload['claimant_principal_id']!r}."
        ),
    )


def _completion_evidence_required() -> Rule:
    """A completion claim must have at least one linked verification evidence item."""

    return Rule(
        rule_id="completion.evidence-required",
        version="1.0.0",
        description=(
            "A completion claim must have at least one linked verification evidence item."
        ),
        applies_to=lambda payload: payload.get("claim_type") == "completion",
        violated=lambda payload: int(payload.get("linked_verification_count", 0)) < 1,
        rationale=lambda payload: (
            f"Completion claim records {payload.get('linked_verification_count', 0)} "
            "linked verification evidence item(s); at least 1 is required."
        ),
    )


def default_registry() -> PolicyRegistry:
    """Return a registry pre-loaded with the built-in starter rules."""

    registry = PolicyRegistry()
    registry.add(_approval_required_recorded())
    registry.add(_approval_no_self_approval())
    registry.add(_completion_evidence_required())
    return registry
