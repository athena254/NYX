"""Application service wrapping the ingestion gateway with transaction boundaries.

Every method opens its own ``session_scope``. A single item is atomic: either the record, its
idempotency reservations, and its audit entry all commit, or none of them do. A batch is
deliberately **not** transactional as a whole — one bad item must not fail the batch — so each
item gets its own transaction and its own :class:`IngestResult`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nyx.config import Settings
from nyx.domain.claims import Claim
from nyx.domain.enums import SystemStatus
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem
from nyx.domain.systems import MonitoredSystem
from nyx.ingestion.errors import IngestResult
from nyx.ingestion.gateway import IngestionGateway
from nyx.ingestion.limits import enforce_batch_size
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.claims import ClaimRepository
from nyx.storage.repositories.events import EventRepository
from nyx.storage.repositories.evidence import EvidenceRepository
from nyx.storage.repositories.systems import SystemRepository
from nyx.systems.service import get_system, list_systems


class IngestionService:
    """Facade over the ingestion gateway that owns transactions."""

    def __init__(self, session_factory: SessionFactory, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self._session_factory = session_factory
        self._gateway = IngestionGateway(self._settings)

    @property
    def settings(self) -> Settings:
        """Return the effective settings used by this service."""

        return self._settings

    def register_system(self, payload: Any, *, request_id: str) -> IngestResult:
        with session_scope(self._session_factory) as session:
            return self._gateway.ingest_system(session, payload, request_id=request_id)

    def ingest_event(
        self,
        payload: Any,
        *,
        request_id: str,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        with session_scope(self._session_factory) as session:
            return self._gateway.ingest_event(
                session, payload, request_id=request_id, idempotency_key=idempotency_key
            )

    def ingest_event_batch(
        self,
        payloads: list[Any],
        *,
        request_id: str,
    ) -> list[IngestResult]:
        """Ingest a bounded batch, returning one outcome per item.

        Raises :class:`nyx.ingestion.limits.LimitExceededError` when the batch itself is too
        large. Per-item keys come from each item's ``producer_event_id``; a whole-request
        ``Idempotency-Key`` is intentionally not applied to every item, which would collapse
        the batch into a single duplicate.
        """

        enforce_batch_size(len(payloads), self._settings)
        results: list[IngestResult] = []
        for payload in payloads:
            with session_scope(self._session_factory) as session:
                results.append(self._gateway.ingest_event(session, payload, request_id=request_id))
        return results

    def register_claim(
        self,
        payload: Any,
        *,
        request_id: str,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        with session_scope(self._session_factory) as session:
            return self._gateway.ingest_claim(
                session, payload, request_id=request_id, idempotency_key=idempotency_key
            )

    def register_evidence(
        self,
        payload: Any,
        *,
        request_id: str,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        with session_scope(self._session_factory) as session:
            return self._gateway.ingest_evidence(
                session, payload, request_id=request_id, idempotency_key=idempotency_key
            )

    def get_event(self, event_id: str) -> Event | None:
        with session_scope(self._session_factory) as session:
            return EventRepository(session).get(event_id)

    def get_claim(self, claim_id: str) -> Claim | None:
        with session_scope(self._session_factory) as session:
            return ClaimRepository(session).get(claim_id)

    def get_evidence(self, evidence_id: str) -> EvidenceItem | None:
        with session_scope(self._session_factory) as session:
            return EvidenceRepository(session).get(evidence_id)

    def list_systems(
        self, *, status: SystemStatus | None = None, limit: int = 100, offset: int = 0
    ) -> list[MonitoredSystem]:
        with session_scope(self._session_factory) as session:
            return list_systems(session, status=status, limit=limit, offset=offset)

    def list_systems_page(
        self,
        *,
        status: SystemStatus | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> list[MonitoredSystem]:
        with session_scope(self._session_factory) as session:
            return list(
                SystemRepository(session).list_after(after=after, limit=limit, status=status)
            )

    def get_system(self, system_id: str) -> MonitoredSystem | None:
        with session_scope(self._session_factory) as session:
            return get_system(session, system_id)
