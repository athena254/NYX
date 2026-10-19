"""Repository for events."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.domain.events import Event
from nyx.storage.repositories._mapping import as_json, as_utc
from nyx.storage.tables import EventRow


class EventRepository:
    """Persistence operations for ``Event``. Never commits implicitly."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, event: Event) -> Event:
        row = EventRow(
            id=event.id,
            schema_version=event.schema_version,
            source_system_id=event.source_system_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            received_at=event.received_at,
            correlation_id=event.correlation_id,
            parent_event_id=event.parent_event_id,
            actor=None if event.actor is None else as_json(event.actor),
            subject=None if event.subject is None else as_json(event.subject),
            payload=dict(event.payload),
            source_ref=event.source_ref,
            content_hash=event.content_hash,
            trust_class=event.trust_class,
            labels=list(event.labels),
            retention_class=event.retention_class,
        )
        self._session.add(row)
        return event

    def get(self, event_id: str) -> Event | None:
        row = self._session.get(EventRow, event_id)
        return None if row is None else _to_domain(row)

    def list(
        self,
        *,
        source_system_id: str | None = None,
        event_type: str | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Event]:
        statement = select(EventRow)
        if source_system_id is not None:
            statement = statement.where(EventRow.source_system_id == source_system_id)
        if event_type is not None:
            statement = statement.where(EventRow.event_type == event_type)
        if correlation_id is not None:
            statement = statement.where(EventRow.correlation_id == correlation_id)
        statement = (
            statement.order_by(EventRow.occurred_at, EventRow.id).limit(limit).offset(offset)
        )
        return [_to_domain(row) for row in self._session.scalars(statement)]

    def find_by_producer_id(self, producer_id: str) -> Event | None:
        """Look up by the producer-supplied correlation id.

        Phase 0's event schema does not contain a dedicated producer event id, so the
        correlation id is the stable producer reference available in Phase 1a.
        """

        statement = (
            select(EventRow)
            .where(EventRow.correlation_id == producer_id)
            .order_by(EventRow.received_at, EventRow.id)
            .limit(1)
        )
        row = self._session.scalars(statement).first()
        return None if row is None else _to_domain(row)


def _to_domain(row: EventRow) -> Event:
    return Event.model_validate(
        {
            "id": row.id,
            "schema_version": row.schema_version,
            "source_system_id": row.source_system_id,
            "event_type": row.event_type,
            "occurred_at": as_utc(row.occurred_at),
            "received_at": as_utc(row.received_at),
            "correlation_id": row.correlation_id,
            "parent_event_id": row.parent_event_id,
            "actor": row.actor,
            "subject": row.subject,
            "payload": row.payload,
            "source_ref": row.source_ref,
            "content_hash": row.content_hash,
            "trust_class": row.trust_class,
            "labels": row.labels,
            "retention_class": row.retention_class,
        }
    )
