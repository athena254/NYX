"""Tests for the authorisation aggregate and the authorisation read path.

The three cases the brief names are the three a bare ``approval_ref`` field cannot express, and
each has its own test: the approver must differ from the actor, the scope must cover the action,
and the approval must not have expired. A record that cannot satisfy the first is refused at
construction; the other two are queries, because an expired or out-of-scope authorisation is
still evidence that someone approved something.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nyx.domain.authorisation import Authorisation
from nyx.domain.base import PrincipalRef, utc_now
from nyx.domain.enums import Environment, PrincipalKind, RiskAction

_ACTOR = PrincipalRef(id="agent:claude", kind=PrincipalKind.AGENT)
_APPROVER = PrincipalRef(id="human:operator", kind=PrincipalKind.HUMAN)


def _authorisation(
    *,
    actor: PrincipalRef = _ACTOR,
    approver: PrincipalRef = _APPROVER,
    scope_ref: str = "booking-service",
    environment: Environment = Environment.STAGING,
    expires_at: datetime | None = None,
    approved_at: datetime | None = None,
) -> Authorisation:
    return Authorisation(
        action="deploy.production",
        risk_action=RiskAction.DEPLOY,
        actor=actor,
        approver=approver,
        environment=environment,
        scope_ref=scope_ref,
        approved_at=approved_at or utc_now(),
        expires_at=expires_at,
    )


# --- the record -----------------------------------------------------------


def test_an_authorisation_names_both_parties() -> None:
    record = _authorisation()
    assert record.actor.id == "agent:claude"
    assert record.approver.id == "human:operator"
    assert record.risk_action is RiskAction.DEPLOY


def test_an_authorisation_may_be_open_ended() -> None:
    """No expiry is a valid approval; it is not an error, and it never lapses."""

    assert _authorisation().expires_at is None
    assert _authorisation().is_expired() is False


def test_self_approval_is_refused_at_construction() -> None:
    """A record that names the actor as its own approver is not an authorisation.

    Refused rather than stored-and-flagged because the record type is the *evidence*; a malformed
    one would be a finding about nothing. Self-approval remains detectable by the actor/approver
    comparison a producer supplies elsewhere.
    """

    with pytest.raises(ValueError, match="approver distinct from the actor"):
        _authorisation(approver=_ACTOR)


def test_an_authorisation_carries_the_scope_it_covers() -> None:
    assert _authorisation(scope_ref="booking-service").scope_ref == "booking-service"


def test_an_authorisation_records_who_granted_it_when_it_is_a_probe_authorisation() -> None:
    record = _authorisation().model_copy(
        update={"granted_by": PrincipalRef(id="human:operator", kind=PrincipalKind.HUMAN)}
    )
    assert record.granted_by is not None
    assert record.granted_by.id == "human:operator"


# --- expiry ---------------------------------------------------------------


def test_an_unexpired_authorisation_is_not_expired() -> None:
    record = _authorisation(expires_at=datetime.now(UTC) + timedelta(hours=1))
    assert record.is_expired() is False


def test_an_expired_authorisation_is_expired() -> None:
    record = _authorisation(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert record.is_expired() is True


def test_expiry_is_checked_against_a_supplied_time_not_only_now() -> None:
    """A replay must be able to ask "was this valid *then*", not only "is it valid now"."""

    record = _authorisation(expires_at=datetime(2026, 10, 25, 12, 0, tzinfo=UTC))
    before = datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
    after = datetime(2026, 10, 25, 13, 0, tzinfo=UTC)
    assert record.is_expired(before) is False
    assert record.is_expired(after) is True


# --- scope matching -------------------------------------------------------


def test_a_matching_scope_is_accepted() -> None:
    assert _authorisation(scope_ref="booking-service").scope_matches("booking-service") is True


def test_a_different_scope_is_rejected() -> None:
    """An approval for one service does not authorise an action on another."""

    assert _authorisation(scope_ref="booking-service").scope_matches("payments-service") is False


def test_scope_matching_ignores_case_and_collapses_whitespace() -> None:
    """A stray space must not fake a mismatch that would refuse a valid approval."""

    record = _authorisation(scope_ref="  Booking   Service ")
    assert record.scope_matches("booking service") is True


def test_scope_matching_is_a_flat_string_comparison_not_a_grammar() -> None:
    """Documented behaviour, not an accident: the authorisation records what a human wrote.

    A parser would turn the match into a judgement; a comparison keeps it a check. Pinned by a
    test so a later "improvement" to a scope grammar has to be a deliberate change.
    """

    record = _authorisation(scope_ref="service:*")
    assert record.scope_matches("service:*") is True
    assert record.scope_matches("service:bookings") is False


# --- the risk taxonomy ----------------------------------------------------


def test_the_risk_taxonomy_has_the_seven_named_categories() -> None:
    assert {action.value for action in RiskAction} == {
        "delete",
        "overwrite_production_state",
        "deploy",
        "spend",
        "external_send",
        "permission_change",
        "secret_access",
    }


def test_every_risk_category_can_carry_an_authorisation() -> None:
    for risk_action in RiskAction:
        record = _authorisation().model_copy(update={"risk_action": risk_action})
        assert record.risk_action is risk_action


def test_an_authorisation_stores_the_action_it_covers() -> None:
    record = _authorisation().model_copy(update={"action": "delete.records"})
    assert record.action == "delete.records"


def test_an_authorisation_names_the_environment_it_covers() -> None:
    """A staging approval must not be read as a production one."""

    record = _authorisation(environment=Environment.STAGING)
    assert record.environment is Environment.STAGING
