"""Scenario 3: invalid schema is rejected with a structured error and no partial write."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.audit import AuditRepository
from nyx.storage.repositories.events import EventRepository

FIXTURES = Path(__file__).resolve().parents[2] / "examples" / "ghost-factory"


def _valid_event() -> dict[str, Any]:
    document = json.loads((FIXTURES / "event-completion-claim.json").read_text(encoding="utf-8"))
    document.pop("id", None)
    document["producer_event_id"] = "validation-valid-1"
    return document


def _counts(session_factory: SessionFactory) -> tuple[int, int]:
    with session_scope(session_factory) as session:
        return len(EventRepository(session).list()), len(AuditRepository(session).list())


def test_invalid_schema_is_rejected_without_a_partial_write(
    service: IngestionService, session_factory: SessionFactory
) -> None:
    invalid = _valid_event()
    del invalid["event_type"]

    result = service.ingest_event(invalid, request_id="req-invalid")

    assert result.status == "rejected"
    assert result.error is not None
    assert result.error.code == "SCHEMA_VALIDATION_FAILED"
    assert result.error.field_details
    assert any(detail.field == "/event_type" for detail in result.error.field_details)
    assert _counts(session_factory) == (0, 0)


def test_a_rejected_item_does_not_touch_a_previous_accepted_item(
    service: IngestionService, session_factory: SessionFactory
) -> None:
    accepted = service.ingest_event(_valid_event(), request_id="req-valid")
    assert accepted.status == "created"

    invalid = _valid_event()
    invalid["producer_event_id"] = "validation-invalid-2"
    invalid["trust_class"] = "totally-trusted"

    rejected = service.ingest_event(invalid, request_id="req-invalid-2")

    assert rejected.status == "rejected"
    assert rejected.error is not None
    assert rejected.error.code == "SCHEMA_VALIDATION_FAILED"
    assert _counts(session_factory)[0] == 1


def test_api_rejects_invalid_body_with_structured_error_and_no_row(
    client: TestClient, session_factory: SessionFactory
) -> None:
    invalid = _valid_event()
    del invalid["occurred_at"]

    response = client.post("/api/v1/events", json=invalid)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "SCHEMA_VALIDATION_FAILED"
    assert body["request_id"]
    assert response.headers["X-Request-Id"] == body["request_id"]
    assert _counts(session_factory)[0] == 0


def test_api_returns_invalid_request_for_unparseable_body(client: TestClient) -> None:
    response = client.post(
        "/api/v1/events",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
