"""Acceptance scenario 22: a policy change records the new version and who changed it.

``PolicyRegistry`` alone cannot satisfy this: it is in-memory and keeps no history at all, so a
change has no actor and no timestamp. These tests exercise the ``PolicyVersionStore`` that supplies
the missing half, and the invariant that matters most -- a policy cannot become active without a
recorded change.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nyx.domain.base import utc_now
from nyx.policies.engine import PolicyDecision, Rule, default_registry
from nyx.policies.versions import (
    DuplicatePolicyVersionError,
    MissingPolicyActorError,
    PolicyChangeError,
    PolicyVersionStore,
)
from nyx.storage.db import session_scope

_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _rule(version: str = "2.0.0") -> Rule:
    return Rule(
        rule_id="approval.no-self-approval",
        version=version,
        description="The approver must not be the claimant.",
        applies_to=lambda payload: payload.get("claimant_principal_id") is not None,
        violated=lambda payload: (
            payload["claimant_principal_id"] == payload.get("approver_principal_id")
        ),
        rationale=lambda payload: "Approver is the claimant.",
    )


def test_registering_a_policy_change_records_version_actor_and_reason(
    session_factory: object,
) -> None:
    registry = default_registry()
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        store = PolicyVersionStore(session, clock=lambda: _NOW)
        version = store.register(
            registry,
            _rule("2.0.0"),
            changed_by="principal_operator",
            change_reason="Tighten the self-approval rule.",
        )

        assert version.rule_id == "approval.no-self-approval"
        assert version.version == "2.0.0"
        assert version.changed_by == "principal_operator"
        assert version.changed_at == _NOW
        assert version.change_reason == "Tighten the self-approval rule."
        assert version.digest.startswith("sha256:")

        recorded = store.latest("approval.no-self-approval")
        assert recorded is not None
        assert recorded.id == version.id


def test_the_registered_rule_is_live_and_its_digest_is_stable(session_factory: object) -> None:
    """The point of the coupling: recording a change is what makes the rule active."""

    registry = default_registry()
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        store = PolicyVersionStore(session, clock=lambda: _NOW)
        version = store.register(
            registry,
            _rule("2.0.0"),
            changed_by="principal_operator",
            change_reason="Tighten the self-approval rule.",
        )

        # The rule is now in force under the version that was recorded...
        evaluation = registry.evaluate(
            "approval.no-self-approval",
            {"claimant_principal_id": "p1", "approver_principal_id": "p2"},
            version="2.0.0",
        )
        assert evaluation.rule_version == "2.0.0"
        assert evaluation.decision is PolicyDecision.SATISFIED

        # ...and the recorded digest matches the live rule revision.
        assert version.digest == registry.digest("approval.no-self-approval", "2.0.0")


def test_a_new_version_supersedes_the_old_one_and_both_remain_readable(
    session_factory: object,
) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        store = PolicyVersionStore(session, clock=lambda: _NOW)
        first = store.register(
            default_registry(),
            _rule("1.0.0"),
            changed_by="principal_operator",
            change_reason="Initial registration.",
        )
        later = store.register(
            default_registry(),
            _rule("1.1.0"),
            changed_by="principal_auditor",
            change_reason="Broaden the rule to service principals.",
        )

        assert store.latest("approval.no-self-approval") is not None
        assert store.latest("approval.no-self-approval").id == later.id
        # The earlier change is still there: history is append-only.
        assert store.latest("approval.no-self-approval").version == "1.1.0"
        history = store.list(rule_id="approval.no-self-approval")
        assert [item.id for item in history] == [later.id, first.id]


def test_a_policy_change_without_an_actor_is_refused(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        store = PolicyVersionStore(session, clock=lambda: _NOW)

        with pytest.raises(MissingPolicyActorError):
            store.register(
                default_registry(),
                _rule("2.0.0"),
                changed_by="",
                change_reason="Tighten the self-approval rule.",
            )


def test_a_policy_change_without_a_reason_is_refused(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        store = PolicyVersionStore(session, clock=lambda: _NOW)

        with pytest.raises(PolicyChangeError, match="why"):
            store.register(
                default_registry(),
                _rule("2.0.0"),
                changed_by="principal_operator",
                change_reason="   ",
            )


def test_an_already_recorded_version_cannot_be_recorded_again(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        store = PolicyVersionStore(session, clock=lambda: _NOW)
        store.register(
            default_registry(),
            _rule("2.0.0"),
            changed_by="principal_operator",
            change_reason="Initial registration.",
        )

        with pytest.raises(DuplicatePolicyVersionError):
            store.register(
                default_registry(),
                _rule("2.0.0"),
                changed_by="principal_auditor",
                change_reason="Try to rewrite the same version.",
            )


def test_a_policy_change_is_recorded_under_the_clock_it_was_given(session_factory: object) -> None:
    """The timestamp is a fact about when the change happened, supplied by the caller's clock."""

    moments = iter([_NOW, _NOW.replace(hour=13)])
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        store = PolicyVersionStore(session, clock=lambda: next(moments))
        first = store.register(
            default_registry(),
            _rule("1.0.0"),
            changed_by="principal_operator",
            change_reason="Initial registration.",
        )
        second = store.register(
            default_registry(),
            _rule("1.1.0"),
            changed_by="principal_operator",
            change_reason="Broaden the rule.",
        )

        assert first.changed_at == _NOW
        assert second.changed_at == _NOW.replace(hour=13)


def test_listing_by_actor_returns_only_their_changes(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        store = PolicyVersionStore(session, clock=lambda: _NOW)
        store.register(
            default_registry(),
            _rule("1.0.0"),
            changed_by="principal_operator",
            change_reason="Initial registration.",
        )
        store.register(
            default_registry(),
            _rule("1.1.0"),
            changed_by="principal_auditor",
            change_reason="Broaden the rule.",
        )

        operator_changes = store.list(changed_by="principal_operator")
        assert [item.changed_by for item in operator_changes] == ["principal_operator"]


def test_a_never_changed_rule_has_no_recorded_version(session_factory: object) -> None:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        store = PolicyVersionStore(session, clock=utc_now)
        assert store.latest("completion.evidence-required") is None
        assert store.version_in_force("completion.evidence-required") is None
