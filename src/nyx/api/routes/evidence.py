"""Evidence registration endpoint (SPEC §9.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response

from nyx.api.deps import get_service
from nyx.api.errors import ApiError, api_error_from, request_id_for
from nyx.domain.evidence import EvidenceItem
from nyx.error_codes import ErrorCode
from nyx.ingestion.inputs import EvidenceInput
from nyx.ingestion.service import IngestionService

router = APIRouter(prefix="/evidence", tags=["evidence"])


@router.post("", response_model=EvidenceItem, status_code=201)
def register_evidence(
    evidence: EvidenceInput,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    service: IngestionService = Depends(get_service),
) -> EvidenceItem:
    """Register an evidence item or reference."""

    result = service.register_evidence(
        evidence, request_id=request_id_for(request), idempotency_key=idempotency_key
    )
    if result.status != "created":
        assert result.error is not None  # nosec B101 - invariant for non-created results
        raise api_error_from(result.error, request)
    assert isinstance(result.record, EvidenceItem)  # nosec B101 - set by the gateway
    return result.record


@router.get("/{evidence_id}", response_model=EvidenceItem)
def get_evidence(
    evidence_id: str,
    request: Request,
    service: IngestionService = Depends(get_service),
) -> EvidenceItem:
    """Retrieve one evidence item by its stable id."""

    evidence = service.get_evidence(evidence_id)
    if evidence is None:
        raise ApiError(ErrorCode.NOT_FOUND, message="Evidence not found.")
    return evidence
