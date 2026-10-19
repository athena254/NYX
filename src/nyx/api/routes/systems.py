"""System registration and listing endpoints (SPEC §9.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response

from nyx.api.deps import get_service
from nyx.api.errors import ApiError, api_error_from, request_id_for
from nyx.api.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from nyx.api.schemas import SystemPage
from nyx.domain.enums import SystemStatus
from nyx.domain.systems import MonitoredSystem
from nyx.error_codes import ErrorCode
from nyx.ingestion.inputs import SystemManifestInput
from nyx.ingestion.service import IngestionService

router = APIRouter(prefix="/systems", tags=["systems"])


@router.post("", response_model=MonitoredSystem, status_code=201)
def create_system(
    manifest: SystemManifestInput,
    request: Request,
    response: Response,
    service: IngestionService = Depends(get_service),
) -> MonitoredSystem:
    """Register a monitored system. Registration is idempotent on name + environment."""

    result = service.register_system(manifest, request_id=request_id_for(request))
    if result.status == "rejected":
        assert result.error is not None  # nosec B101 - invariant of a rejected result
        raise api_error_from(result.error, request)
    response.status_code = 201 if result.status == "created" else 200
    assert isinstance(result.record, MonitoredSystem)  # nosec B101 - set by the gateway
    return result.record


@router.get("", response_model=SystemPage)
def list_systems(
    request: Request,
    status: SystemStatus | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1),
    service: IngestionService = Depends(get_service),
) -> SystemPage:
    """List systems visible to the caller with deterministic cursor pagination."""

    after = None
    if cursor is not None:
        try:
            after = decode_cursor(cursor)
        except InvalidCursorError as exc:
            raise ApiError(ErrorCode.INVALID_REQUEST, message="The cursor is not valid.") from exc
    effective_limit = clamp_limit(limit)
    page = service.list_systems_page(status=status, after=after, limit=effective_limit + 1)
    items = page[:effective_limit]
    next_cursor = (
        encode_cursor(items[-1].created_at, items[-1].id) if len(page) > effective_limit else None
    )
    return SystemPage(items=items, next_cursor=next_cursor)
