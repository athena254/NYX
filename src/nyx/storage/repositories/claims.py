"""Repository for claims."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.domain.claims import Claim
from nyx.domain.enums import ClaimStatus
from nyx.storage.repositories._mapping import as_json, as_utc
from nyx.storage.tables import ClaimRow


class ClaimRepository:
    """Persistence operations for ``Claim``. Never commits implicitly."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, claim: Claim) -> Claim:
        row = ClaimRow(
            id=claim.id,
            schema_version=claim.schema_version,
            statement=claim.statement,
            claimant_system_id=claim.claimant_system_id,
            claimant_principal_id=claim.claimant_principal_id,
            claim_type=claim.claim_type,
            scope=as_json(claim.scope),
            created_at=claim.created_at,
            source_event_ids=list(claim.source_event_ids),
            status=claim.status,
        )
        self._session.add(row)
        return claim

    def get(self, claim_id: str) -> Claim | None:
        row = self._session.get(ClaimRow, claim_id)
        return None if row is None else _to_domain(row)

    def list(
        self,
        *,
        claimant_system_id: str | None = None,
        status: ClaimStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Claim]:
        statement = select(ClaimRow)
        if claimant_system_id is not None:
            statement = statement.where(ClaimRow.claimant_system_id == claimant_system_id)
        if status is not None:
            statement = statement.where(ClaimRow.status == status.value)
        statement = statement.order_by(ClaimRow.created_at, ClaimRow.id).limit(limit).offset(offset)
        return [_to_domain(row) for row in self._session.scalars(statement)]

    def find_by_producer_id(self, producer_id: str) -> Claim | None:
        """Return the most recent claim attributed to a producer system.

        The Phase 0 claim schema has no per-record producer claim id. The claimant system id
        is the stable producer reference available in Phase 1a; callers that need exact
        replay semantics should use an idempotency key in the ingestion layer (Phase 1b).
        """

        statement = (
            select(ClaimRow)
            .where(ClaimRow.claimant_system_id == producer_id)
            .order_by(ClaimRow.created_at.desc(), ClaimRow.id.desc())
            .limit(1)
        )
        row = self._session.scalars(statement).first()
        return None if row is None else _to_domain(row)


def _to_domain(row: ClaimRow) -> Claim:
    return Claim.model_validate(
        {
            "id": row.id,
            "schema_version": row.schema_version,
            "statement": row.statement,
            "claimant_system_id": row.claimant_system_id,
            "claimant_principal_id": row.claimant_principal_id,
            "claim_type": row.claim_type,
            "scope": row.scope,
            "created_at": as_utc(row.created_at),
            "source_event_ids": row.source_event_ids,
            "status": row.status,
        }
    )
