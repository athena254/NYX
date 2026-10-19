"""Claim registration endpoint (SPEC §9.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response

from nyx.api.deps import get_service
from nyx.api.errors import ApiError, api_error_from, request_id_for
from nyx.domain.claims import Claim
from nyx.error_codes import ErrorCode
from nyx.ingestion.inputs import ClaimInput
from nyx.ingestion.service import IngestionService

router = APIRouter(prefix="/claims", tags=["claims"])


@router.post("", response_model=Claim, status_code=201)
def register_claim(
    claim: ClaimInput,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    service: IngestionService = Depends(get_service),
) -> Claim:
    """Register a scoped claim."""

    result = service.register_claim(
        claim, request_id=request_id_for(request), idempotency_key=idempotency_key
    )
    if result.status != "created":
        assert result.error is not None  # nosec B101 - invariant for non-created results
        raise api_error_from(result.error, request)
    assert isinstance(result.record, Claim)  # nosec B101 - set by the gateway
    return result.record


@router.get("/{claim_id}", response_model=Claim)
def get_claim(
    claim_id: str,
    request: Request,
    service: IngestionService = Depends(get_service),
) -> Claim:
    """Retrieve one claim by its stable id."""

    claim = service.get_claim(claim_id)
    if claim is None:
        raise ApiError(ErrorCode.NOT_FOUND, message="Claim not found.")
    return claim
