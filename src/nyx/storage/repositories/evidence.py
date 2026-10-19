"""Repository for evidence items."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.domain.enums import Sensitivity
from nyx.domain.evidence import EvidenceItem
from nyx.storage.repositories._mapping import as_json, as_utc
from nyx.storage.tables import EvidenceRow


class EvidenceRepository:
    """Persistence operations for ``EvidenceItem``. Never commits implicitly."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, evidence: EvidenceItem) -> EvidenceItem:
        row = EvidenceRow(
            id=evidence.id,
            schema_version=evidence.schema_version,
            kind=evidence.kind,
            title=evidence.title,
            description=evidence.description,
            source_system_id=evidence.source_system_id,
            source_name=evidence.source_name,
            source_ref=evidence.source_ref,
            locator=evidence.locator,
            captured_at=evidence.captured_at,
            valid_from=evidence.valid_from,
            valid_until=evidence.valid_until,
            content_hash=evidence.content_hash,
            integrity_method=evidence.integrity_method,
            producer_identity=(
                None if evidence.producer_identity is None else as_json(evidence.producer_identity)
            ),
            verification=(
                None if evidence.verification is None else as_json(evidence.verification)
            ),
            relevance_to_claim=evidence.relevance_to_claim,
            relevance_to_finding=evidence.relevance_to_finding,
            relevance_note=evidence.relevance_note,
            limitations=list(evidence.limitations),
            sensitivity=evidence.sensitivity,
            access_policy_ref=evidence.access_policy_ref,
            retention_class=evidence.retention_class,
        )
        self._session.add(row)
        return evidence

    def get(self, evidence_id: str) -> EvidenceItem | None:
        row = self._session.get(EvidenceRow, evidence_id)
        return None if row is None else _to_domain(row)

    def list(
        self,
        *,
        source_system_id: str | None = None,
        sensitivity: Sensitivity | None = None,
        relevance_to_claim: str | None = None,
        relevance_to_finding: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EvidenceItem]:
        statement = select(EvidenceRow)
        if source_system_id is not None:
            statement = statement.where(EvidenceRow.source_system_id == source_system_id)
        if sensitivity is not None:
            statement = statement.where(EvidenceRow.sensitivity == sensitivity.value)
        if relevance_to_claim is not None:
            statement = statement.where(EvidenceRow.relevance_to_claim == relevance_to_claim)
        if relevance_to_finding is not None:
            statement = statement.where(EvidenceRow.relevance_to_finding == relevance_to_finding)
        statement = (
            statement.order_by(EvidenceRow.captured_at, EvidenceRow.id).limit(limit).offset(offset)
        )
        return [_to_domain(row) for row in self._session.scalars(statement)]

    def find_by_producer_id(self, producer_id: str) -> EvidenceItem | None:
        """Look up by the producer-supplied source reference."""

        statement = (
            select(EvidenceRow)
            .where(EvidenceRow.source_ref == producer_id)
            .order_by(EvidenceRow.captured_at, EvidenceRow.id)
            .limit(1)
        )
        row = self._session.scalars(statement).first()
        return None if row is None else _to_domain(row)


def _to_domain(row: EvidenceRow) -> EvidenceItem:
    return EvidenceItem.model_validate(
        {
            "id": row.id,
            "schema_version": row.schema_version,
            "kind": row.kind,
            "title": row.title,
            "description": row.description,
            "source_system_id": row.source_system_id,
            "source_name": row.source_name,
            "source_ref": row.source_ref,
            "locator": row.locator,
            "captured_at": as_utc(row.captured_at),
            "valid_from": as_utc(row.valid_from),
            "valid_until": None if row.valid_until is None else as_utc(row.valid_until),
            "content_hash": row.content_hash,
            "integrity_method": row.integrity_method,
            "producer_identity": row.producer_identity,
            "verification": row.verification,
            "relevance_to_claim": row.relevance_to_claim,
            "relevance_to_finding": row.relevance_to_finding,
            "relevance_note": row.relevance_note,
            "limitations": row.limitations,
            "sensitivity": row.sensitivity,
            "access_policy_ref": row.access_policy_ref,
            "retention_class": row.retention_class,
        }
    )
