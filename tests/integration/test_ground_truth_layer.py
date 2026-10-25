"""Integration tests for the ground-truth layer against a real migrated database.

The unit tests prove the records validate. These prove the rest of the system can *consume*
them: that the migration creates the tables the repositories read, that an observation survives a
round trip, and that ``list_covering`` -- the read an upgraded C-001 depends on -- filters on
strength and version the way the rule requires.

The distinction matters because it is the seam where this project has lost defects before: a
slice's own tests assert the shape of what it returns, not that the rest of the system can use it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nyx.domain.authorisation import Authorisation
from nyx.domain.base import PrincipalRef, utc_now
from nyx.domain.enums import (
    Environment,
    PrincipalKind,
    ProbeKind,
    ProbeOutcome,
    RiskAction,
    VerificationStrength,
)
from nyx.domain.observation import Observation
from nyx.storage.repositories.authorisations import AuthorisationRepository
from nyx.storage.repositories.observations import ObservationRepository
from nyx.storage.tables import ObservationRow

pytestmark = pytest.mark.usefixtures("migration_template")


def _observation(
    *,
    observation_id: str,
    subject: str = "booking-service",
    subject_version: str | None = "abc123",
    strength: VerificationStrength = VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
    claim_id: str | None = "clm_1",
) -> Observation:
    return Observation(
        id=observation_id,
        probe_id="probe:state-check",
        probe_version="1.0.0",
        subject=subject,
        subject_version=subject_version,
        collected_at=utc_now(),
        collector=PrincipalRef(id="nyx:probe", kind=PrincipalKind.SYSTEM),
        method=ProbeKind.STATE_CHECK,
        result=ProbeOutcome.OBSERVED,
        raw_artifact_hash="sha256:" + "a" * 64,
        environment=Environment.STAGING,
        read_only=True,
        verification_strength=strength,
        relevance_to_claim=claim_id,
        detail="row booking_17 exists",
        payload={"rows": [{"id": "booking_17"}]},
    )


def _authorisation(
    *,
    authorisation_id: str,
    scope_ref: str = "booking-service",
    expires_at: datetime | None = None,
) -> Authorisation:
    return Authorisation(
        id=authorisation_id,
        action="deploy.staging",
        risk_action=RiskAction.DEPLOY,
        actor=PrincipalRef(id="agent:claude", kind=PrincipalKind.AGENT),
        approver=PrincipalRef(id="human:operator", kind=PrincipalKind.HUMAN),
        environment=Environment.STAGING,
        scope_ref=scope_ref,
        approved_at=utc_now(),
        expires_at=expires_at,
    )


# --- the migration --------------------------------------------------------


def test_the_migration_creates_both_tables(session_factory: object) -> None:
    """A repository read is meaningless if the migration never created the table."""

    from sqlalchemy import inspect

    from nyx.storage.db import session_scope

    assert session_factory is not None
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        names = set(inspect(session.bind).get_table_names())
    assert {"observations", "authorisations"} <= names


def test_the_migration_indexes_the_columns_the_critics_filter_on(
    session_factory: object,
) -> None:
    from sqlalchemy import inspect

    from nyx.storage.db import session_scope

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        indexed = {
            column
            for index in inspect(session.bind).get_indexes("observations")
            for column in index["column_names"]
        }
    assert {"subject", "subject_version", "verification_strength", "relevance_to_claim"} <= indexed


# --- observation persistence ---------------------------------------------


def test_an_observation_survives_a_round_trip(session_factory: object) -> None:
    from nyx.storage.db import session_scope

    stored = _observation(observation_id="obs_roundtrip")
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        ObservationRepository(session).add(stored)
        session.commit()

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        loaded = ObservationRepository(session).get("obs_roundtrip")

    assert loaded is not None
    assert loaded.probe_id == "probe:state-check"
    assert loaded.subject_version == "abc123"
    assert loaded.verification_strength is VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE
    assert loaded.raw_artifact_hash == "sha256:" + "a" * 64
    assert loaded.payload == {"rows": [{"id": "booking_17"}]}
    assert loaded.read_only is True


def test_a_claim_linked_observation_is_retrieved_by_claim(session_factory: object) -> None:
    """The read the critic registry makes: observations gathered against one claim."""

    from nyx.storage.db import session_scope

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = ObservationRepository(session)
        repository.add(_observation(observation_id="obs_for_clm1", claim_id="clm_1"))
        repository.add(_observation(observation_id="obs_for_clm2", claim_id="clm_2"))
        session.commit()

        found = repository.list(relevance_to_claim="clm_1")

    assert [item.id for item in found] == ["obs_for_clm1"]


def test_list_covering_filters_on_strength(session_factory: object) -> None:
    """The gate an upgraded C-001 applies: only observations strong enough may verify a claim."""

    from nyx.storage.db import session_scope

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = ObservationRepository(session)
        repository.add(
            _observation(
                observation_id="obs_weak",
                strength=VerificationStrength.SELF_REPORTED,
            )
        )
        repository.add(
            _observation(
                observation_id="obs_strong",
                strength=VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
            )
        )
        session.commit()

        covering = repository.list_covering(
            subject="booking-service",
            subject_version="abc123",
            minimum_strength=VerificationStrength.RERUN_BY_NYX,
        )

    assert [item.id for item in covering] == ["obs_strong"]


def test_list_covering_requires_a_matching_subject_version(session_factory: object) -> None:
    """An observation of commit def456 cannot verify a claim about abc123."""

    from nyx.storage.db import session_scope

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = ObservationRepository(session)
        repository.add(_observation(observation_id="obs_other_version", subject_version="def456"))
        session.commit()

        covering = repository.list_covering(
            subject="booking-service",
            subject_version="abc123",
            minimum_strength=VerificationStrength.RERUN_BY_NYX,
        )

    assert covering == []


def test_list_covering_ignores_a_subject_when_no_version_is_asked_for(
    session_factory: object,
) -> None:
    from nyx.storage.db import session_scope

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = ObservationRepository(session)
        repository.add(_observation(observation_id="obs_any"))
        session.commit()

        covering = repository.list_covering(
            subject="booking-service",
            subject_version=None,
            minimum_strength=VerificationStrength.RERUN_BY_NYX,
        )

    assert [item.id for item in covering] == ["obs_any"]


def test_observations_are_ordered_by_collection_time(session_factory: object) -> None:
    from nyx.storage.db import session_scope

    earlier = utc_now() - timedelta(minutes=5)
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = ObservationRepository(session)
        repository.add(
            _observation(observation_id="obs_late").model_copy(update={"collected_at": utc_now()})
        )
        repository.add(
            _observation(observation_id="obs_early").model_copy(update={"collected_at": earlier})
        )
        session.commit()

        found = repository.list(subject="booking-service")

    assert [item.id for item in found] == ["obs_early", "obs_late"]


# --- authorisation persistence -------------------------------------------


def test_an_authorisation_survives_a_round_trip(session_factory: object) -> None:
    from nyx.storage.db import session_scope

    stored = _authorisation(authorisation_id="aut_roundtrip")
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        AuthorisationRepository(session).add(stored)
        session.commit()

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        loaded = AuthorisationRepository(session).get("aut_roundtrip")

    assert loaded is not None
    assert loaded.actor.id == "agent:claude"
    assert loaded.approver.id == "human:operator"
    assert loaded.risk_action is RiskAction.DEPLOY
    assert loaded.environment is Environment.STAGING


def test_find_covering_returns_a_valid_authorisation(session_factory: object) -> None:
    from nyx.storage.db import session_scope

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        AuthorisationRepository(session).add(_authorisation(authorisation_id="aut_valid"))
        session.commit()

        found = AuthorisationRepository(session).find_covering(
            actor_id="agent:claude",
            action="deploy.staging",
            risk_action=RiskAction.DEPLOY,
            action_scope="booking-service",
        )

    assert found is not None
    assert found.id == "aut_valid"


def test_find_covering_refuses_an_expired_authorisation(session_factory: object) -> None:
    from nyx.storage.db import session_scope

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        AuthorisationRepository(session).add(
            _authorisation(
                authorisation_id="aut_expired",
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        session.commit()

        found = AuthorisationRepository(session).find_covering(
            actor_id="agent:claude",
            action="deploy.staging",
            risk_action=RiskAction.DEPLOY,
            action_scope="booking-service",
        )

    assert found is None


def test_find_covering_refuses_an_out_of_scope_authorisation(session_factory: object) -> None:
    """A staging approval must not authorise an action on a different service."""

    from nyx.storage.db import session_scope

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        AuthorisationRepository(session).add(
            _authorisation(authorisation_id="aut_other_scope", scope_ref="payments-service")
        )
        session.commit()

        found = AuthorisationRepository(session).find_covering(
            actor_id="agent:claude",
            action="deploy.staging",
            risk_action=RiskAction.DEPLOY,
            action_scope="booking-service",
        )

    assert found is None


def test_find_covering_ignores_another_actor(session_factory: object) -> None:
    from nyx.storage.db import session_scope

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        AuthorisationRepository(session).add(_authorisation(authorisation_id="aut_actor"))
        session.commit()

        found = AuthorisationRepository(session).find_covering(
            actor_id="agent:someone-else",
            action="deploy.staging",
            risk_action=RiskAction.DEPLOY,
            action_scope="booking-service",
        )

    assert found is None


def test_an_authorisation_cannot_be_recorded_twice(session_factory: object) -> None:
    """One approval counted twice would let a critic see more authority than exists.

    The duplicate is flushed inside the ``session_scope`` block so the boundary that owns the
    commit is the one that raises; calling ``commit`` by hand first would leave the session in a
    rolled-back state and surface a ``PendingRollbackError`` instead of the constraint violation.
    """

    from sqlalchemy.exc import IntegrityError

    from nyx.storage.db import session_scope

    fixed_time = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        repository = AuthorisationRepository(session)
        repository.add(
            _authorisation(authorisation_id="aut_first", expires_at=None).model_copy(
                update={"approved_at": fixed_time}
            )
        )
        session.commit()

    duplicate = _authorisation(authorisation_id="aut_second", expires_at=None).model_copy(
        update={"approved_at": fixed_time}
    )
    with (
        pytest.raises(IntegrityError, match="UNIQUE constraint failed: authorisations"),
        session_scope(session_factory) as session,  # type: ignore[arg-type]
    ):
        AuthorisationRepository(session).add(duplicate)


def test_the_observation_row_carries_the_read_only_flag(session_factory: object) -> None:
    """The safety flag must survive persistence, not just validation."""

    from sqlalchemy import select

    from nyx.storage.db import session_scope

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        ObservationRepository(session).add(_observation(observation_id="obs_ro"))
        session.commit()
        row = session.scalars(select(ObservationRow).where(ObservationRow.id == "obs_ro")).one()

    assert bool(row.read_only) is True
