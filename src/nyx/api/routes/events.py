"""Event ingestion endpoints (SPEC §9.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response

from nyx.api.deps import get_service
from nyx.api.errors import ApiError, api_error_from, request_id_for
from nyx.api.schemas import BatchResponse, BatchResultItem, EventBatchRequest
from nyx.domain.events import Event
from nyx.error_codes import ErrorCode
from nyx.ingestion.errors import IngestResult
from nyx.ingestion.inputs import EventInput
from nyx.ingestion.limits import LimitExceededError
from nyx.ingestion.service import IngestionService

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=Event, status_code=201)
def ingest_event(
    event: EventInput,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    service: IngestionService = Depends(get_service),
) -> Event:
    """Ingest one event. A replay of an accepted event returns a conflict envelope."""

    result = service.ingest_event(
        event, request_id=request_id_for(request), idempotency_key=idempotency_key
    )
    if result.status == "duplicate":
        assert result.error is not None  # nosec B101 - invariant of a duplicate result
        raise api_error_from(result.error, request)
    if result.status == "rejected":
        assert result.error is not None  # nosec B101 - invariant of a rejected result
        raise api_error_from(result.error, request)
    assert isinstance(result.record, Event)  # nosec B101 - set by the gateway
    return result.record


@router.get("/{event_id}", response_model=Event)
def get_event(
    event_id: str,
    request: Request,
    service: IngestionService = Depends(get_service),
) -> Event:
    """Retrieve one event by its stable id."""

    event = service.get_event(event_id)
    if event is None:
        raise ApiError(ErrorCode.NOT_FOUND, message="Event not found.")
    return event


@router.post("/batch", response_model=BatchResponse)
def ingest_event_batch(
    payload: EventBatchRequest,
    request: Request,
    service: IngestionService = Depends(get_service),
) -> BatchResponse:
    """Ingest a bounded batch; one bad item does not fail the other items."""

    request_id = request_id_for(request)
    try:
        results = service.ingest_event_batch(payload.events, request_id=request_id)
    except LimitExceededError as exc:
        raise ApiError(
            ErrorCode.BATCH_TOO_LARGE,
            message=(
                f"Batch contains {exc.observed} items, exceeding the configured maximum "
                f"of {exc.limit}."
            ),
        ) from exc
    return BatchResponse(
        results=[_batch_item(index, result) for index, result in enumerate(results)],
        request_id=request_id,
    )


def _batch_item(index: int, result: IngestResult) -> BatchResultItem:
    item = BatchResultItem(index=index, status=result.status, record_id=result.record_id)
    if result.error is not None:
        item.error = result.error
        item.reason = result.error.code
    return item
