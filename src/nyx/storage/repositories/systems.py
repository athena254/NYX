"""Repository for monitored systems."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.domain.enums import Environment, SystemStatus
from nyx.domain.systems import MonitoredSystem
from nyx.storage.repositories._mapping import as_utc
from nyx.storage.tables import SystemRow


class SystemRepository:
    """Persistence operations for ``MonitoredSystem``.

    Methods never commit; the caller owns the transaction boundary.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, system: MonitoredSystem) -> MonitoredSystem:
        row = SystemRow(
            id=system.id,
            schema_version=system.schema_version,
            name=system.name,
            kind=system.kind,
            environment=system.environment,
            owner=system.owner,
            capabilities=list(system.capabilities),
            adapter=system.adapter,
            metadata_json=dict(system.metadata),
            created_at=system.created_at,
            status=system.status,
        )
        self._session.add(row)
        return system

    def get(self, system_id: str) -> MonitoredSystem | None:
        row = self._session.get(SystemRow, system_id)
        return None if row is None else _to_domain(row)

    def list(
        self,
        *,
        status: SystemStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MonitoredSystem]:
        statement = select(SystemRow)
        if status is not None:
            statement = statement.where(SystemRow.status == status.value)
        statement = (
            statement.order_by(SystemRow.created_at, SystemRow.id).limit(limit).offset(offset)
        )
        return [_to_domain(row) for row in self._session.scalars(statement)]

    def find_by_name_environment(
        self, name: str, environment: Environment
    ) -> MonitoredSystem | None:
        statement = (
            select(SystemRow)
            .where(SystemRow.name == name, SystemRow.environment == environment.value)
            .order_by(SystemRow.created_at, SystemRow.id)
            .limit(1)
        )
        row = self._session.scalars(statement).first()
        return None if row is None else _to_domain(row)

    def find_by_producer_id(
        self, producer_id: str, *, environment: Environment | None = None
    ) -> MonitoredSystem | None:
        """Look up by the producer's stable name.

        A monitored system's producer identity is its manifest ``name``; ``environment``
        narrows the lookup when the same name is registered in more than one environment.
        """

        statement = select(SystemRow).where(SystemRow.name == producer_id)
        if environment is not None:
            statement = statement.where(SystemRow.environment == environment.value)
        statement = statement.order_by(SystemRow.created_at, SystemRow.id).limit(1)
        row = self._session.scalars(statement).first()
        return None if row is None else _to_domain(row)


def _to_domain(row: SystemRow) -> MonitoredSystem:
    return MonitoredSystem.model_validate(
        {
            "id": row.id,
            "schema_version": row.schema_version,
            "name": row.name,
            "kind": row.kind,
            "environment": row.environment,
            "owner": row.owner,
            "capabilities": row.capabilities,
            "adapter": row.adapter,
            "metadata": row.metadata_json,
            "created_at": as_utc(row.created_at),
            "status": row.status,
        }
    )
