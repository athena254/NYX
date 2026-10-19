"""Unit tests for the policy engine (SPEC §8, C-005).

Covers: version resolution (latest vs explicit), unknown id/version errors,
NOT_APPLICABLE handling, the recorded deciding version, multi-version coexistence,
and digest stability.
"""

from __future__ import annotations

import pytest

from nyx.policies.engine import (
    PolicyDecision,
    PolicyRegistry,
    Rule,
    UnknownPolicyError,
    default_registry,
)


def _rule(
    rule_id: str = "test.rule",
    version: str = "1.0.0",
    *,
    applies: bool = True,
    violated: bool = False,
    description: str = "A test rule.",
) -> Rule:
    return Rule(
        rule_id=rule_id,
        version=version,
        description=description,
        applies_to=lambda payload: applies,
        violated=lambda payload: violated,
        rationale=lambda payload: f"Decision for {payload!r}.",
    )


# ---------------------------------------------------------------------------
# Resolution: latest vs explicit version, unknown id/version errors.
# ---------------------------------------------------------------------------


def test_get_by_id_resolves_latest_version() -> None:
    registry = PolicyRegistry()
    registry.add(_rule(version="1.0.0"))
    registry.add(_rule(version="1.2.0"))
    registry.add(_rule(version="1.10.0"))

    resolved = registry.get("test.rule")

    assert resolved.version == "1.10.0"


def test_get_by_explicit_version_resolves_exactly() -> None:
    registry = PolicyRegistry()
    registry.add(_rule(version="1.0.0"))
    registry.add(_rule(version="2.0.0"))

    resolved = registry.get("test.rule", version="1.0.0")

    assert resolved.version == "1.0.0"


def test_get_unknown_id_raises() -> None:
    registry = PolicyRegistry()
    registry.add(_rule())

    with pytest.raises(UnknownPolicyError):
        registry.get("no.such-rule")


def test_get_unknown_version_raises() -> None:
    registry = PolicyRegistry()
    registry.add(_rule(version="1.0.0"))

    with pytest.raises(UnknownPolicyError):
        registry.get("test.rule", version="9.9.9")


def test_add_same_id_version_replaces() -> None:
    registry = PolicyRegistry()
    registry.add(_rule(description="first"))
    registry.add(_rule(description="second"))

    rules = registry.list_rules()

    assert len(rules) == 1
    assert rules[0].description == "second"


# ---------------------------------------------------------------------------
# Evaluation semantics.
# ---------------------------------------------------------------------------


def test_evaluate_not_applicable_when_rule_does_not_apply() -> None:
    registry = PolicyRegistry()
    registry.add(_rule(applies=False, violated=True))

    evaluation = registry.evaluate("test.rule", {"any": "payload"})

    assert evaluation.decision is PolicyDecision.NOT_APPLICABLE
    assert evaluation.rule_version == "1.0.0"


def test_evaluate_satisfied_and_violated() -> None:
    registry = PolicyRegistry()
    registry.add(_rule(rule_id="ok.rule", violated=False))
    registry.add(_rule(rule_id="bad.rule", violated=True))

    ok = registry.evaluate("ok.rule", {})
    bad = registry.evaluate("bad.rule", {})

    assert ok.decision is PolicyDecision.SATISFIED
    assert bad.decision is PolicyDecision.VIOLATED


def test_evaluation_records_the_version_that_decided() -> None:
    registry = PolicyRegistry()
    registry.add(_rule(version="1.0.0"))
    registry.add(_rule(version="2.0.0"))

    latest = registry.evaluate("test.rule", {})
    pinned = registry.evaluate("test.rule", {}, version="1.0.0")

    assert latest.rule_version == "2.0.0"
    assert pinned.rule_version == "1.0.0"


def test_evaluate_all_covers_latest_of_each_rule_id() -> None:
    registry = PolicyRegistry()
    registry.add(_rule(rule_id="a.rule", version="1.0.0"))
    registry.add(_rule(rule_id="a.rule", version="2.0.0"))
    registry.add(_rule(rule_id="b.rule", version="1.0.0"))

    evaluations = registry.evaluate_all({})

    assert [(e.rule_id, e.rule_version) for e in evaluations] == [
        ("a.rule", "2.0.0"),
        ("b.rule", "1.0.0"),
    ]


# ---------------------------------------------------------------------------
# Multi-version coexistence.
# ---------------------------------------------------------------------------


def test_two_versions_of_one_rule_id_coexist() -> None:
    registry = PolicyRegistry()
    registry.add(_rule(version="1.0.0"))
    registry.add(_rule(version="2.0.0"))

    rules = registry.list_rules()

    assert [rule.version for rule in rules] == ["1.0.0", "2.0.0"]
    assert registry.get("test.rule").version == "2.0.0"
    assert registry.get("test.rule", version="1.0.0").version == "1.0.0"


# ---------------------------------------------------------------------------
# Digest stability.
# ---------------------------------------------------------------------------


def test_digest_stable_for_same_rule() -> None:
    registry = PolicyRegistry()
    registry.add(_rule())

    assert registry.digest("test.rule") == registry.digest("test.rule")
    assert registry.digest("test.rule", version="1.0.0") == registry.digest("test.rule")


def test_digest_differs_for_changed_rule() -> None:
    registry = PolicyRegistry()
    registry.add(_rule(description="original"))
    before = registry.digest("test.rule")
    registry.add(_rule(description="changed"))

    assert registry.digest("test.rule") != before


def test_default_registry_contains_starter_rules() -> None:
    registry = default_registry()

    assert [rule.rule_id for rule in registry.list_rules()] == [
        "approval.required-recorded",
        "approval.no-self-approval",
        "completion.evidence-required",
    ]
    assert all(rule.version == "1.0.0" for rule in registry.list_rules())
