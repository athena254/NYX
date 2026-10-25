"""Repository for observations. Never commits implicitly."""

from __future__ import annotations

import builtins

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.domain.enums import VerificationStrength
from nyx.domain.observation import Observation
from nyx.storage.repositories._mapping import as_json, as_utc
from nyx.storage.tables import ObservationRow


class ObservationRepository:
    """Persistence operations for :class:`~nyx.domain.observation.Observation`.

    The read that matters is :meth:`list_covering`: "is there an observation of at least this
    strength for this subject version?". An upgraded C-001 asks exactly that before it accepts
    an observation as verification of a completion claim.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, observation: Observation) -> Observation:
        self._session.add(_to_row(observation))
        return observation

    def get(self, observation_id: str) -> Observation | None:
        row = self._session.get(ObservationRow, observation_id)
        return None if row is None else _to_domain(row)

    def list(
        self,
        *,
        subject: str | None = None,
        subject_version: str | None = None,
        relevance_to_claim: str | None = None,
        probe_id: str | None = None,
        read_only: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Observation]:
        statement = select(ObservationRow)
        if subject is not None:
            statement = statement.where(ObservationRow.subject == subject)
        if subject_version is not None:
            statement = statement.where(ObservationRow.subject_version == subject_version)
        if relevance_to_claim is not None:
            statement = statement.where(ObservationRow.relevance_to_claim == relevance_to_claim)
        if probe_id is not None:
            statement = statement.where(ObservationRow.probe_id == probe_id)
        if read_only is not None:
            statement = statement.where(ObservationRow.read_only == read_only)
        statement = (
            statement.order_by(ObservationRow.collected_at, ObservationRow.id)
            .limit(limit)
            .offset(offset)
        )
        return [_to_domain(row) for row in self._session.scalars(statement)]

    def list_covering(
        self,
        *,
        subject: str,
        subject_version: str | None,
        minimum_strength: VerificationStrength,
        limit: int = 100,
    ) -> builtins.list[Observation]:
        """Return observations for ``subject`` that clear ``minimum_strength``.

        ``subject_version`` is matched only when it is supplied: an observation with no declared
        version is not a match, because an undated observation cannot verify a dated claim. That
        is the same rule C-003 already applies to evidence, applied to the stronger record.
        """

        statement = select(ObservationRow).where(ObservationRow.subject == subject)
        if subject_version is not None:
            statement = statement.where(ObservationRow.subject_version == subject_version)
        statement = statement.order_by(ObservationRow.collected_at, ObservationRow.id).limit(limit)
        found: list[Observation] = []
        for row in self._session.scalars(statement):
            domain = _to_domain(row)
            if domain.verification_strength in _at_least(minimum_strength):
                found.append(domain)
        return found


def _at_least(minimum: VerificationStrength) -> frozenset[VerificationStrength]:
    """Return every strength at or above ``minimum`` in the enum's declared order."""

    order = list(VerificationStrength)
    return frozenset(order[order.index(minimum) :])


def _to_row(observation: Observation) -> ObservationRow:
    return ObservationRow(
        id=observation.id,
        schema_version=observation.schema_version,
        probe_id=observation.probe_id,
        probe_version=observation.probe_version,
        subject=observation.subject,
        subject_version=observation.subject_version,
        collected_at=observation.collected_at,
        collector=as_json(observation.collector),
        method=observation.method,
        result=observation.result,
        raw_artifact_hash=observation.raw_artifact_hash,
        environment=observation.environment,
        read_only=observation.read_only,
        verification_strength=observation.verification_strength,
        scope=None if observation.scope is None else as_json(observation.scope),
        relevance_to_claim=observation.relevance_to_claim,
        detail=observation.detail,
        payload=observation.payload,
        note=observation.note,
    )


def _to_domain(row: ObservationRow) -> Observation:
    return Observation.model_validate(
        {
            "id": row.id,
            "schema_version": row.schema_version,
            "probe_id": row.probe_id,
            "probe_version": row.probe_version,
            "subject": row.subject,
            "subject_version": row.subject_version,
            "collected_at": as_utc(row.collected_at),
            "collector": row.collector,
            "method": row.method,
            "result": row.result,
            "raw_artifact_hash": row.raw_artifact_hash,
            "environment": row.environment,
            "read_only": bool(row.read_only),
            "verification_strength": row.verification_strength,
            "scope": row.scope,
            "relevance_to_claim": row.relevance_to_claim,
            "detail": row.detail,
            "payload": row.payload,
            "note": row.note,
        }
    )
