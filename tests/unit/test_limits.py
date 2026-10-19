"""Payload and batch limit enforcement, including the exact-boundary case (SPEC §9.2)."""

from __future__ import annotations

import pytest

from nyx.config import Settings
from nyx.domain.canonical import canonical_json
from nyx.error_codes import ErrorCode
from nyx.ingestion.limits import LimitExceededError, enforce_batch_size, enforce_payload_size
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory


def _event(payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "source_system_id": "sys_limits",
        "event_type": "computation.sample",
        "occurred_at": "2026-10-19T10:00:00Z",
        "correlation_id": "corr-limits",
        "payload": payload,
        "trust_class": "observed",
        "labels": [],
        "retention_class": "standard",
    }


def test_payload_helpers_accept_exactly_the_limit() -> None:
    settings = Settings(max_payload_bytes=100, max_batch_size=2)

    assert enforce_payload_size("x" * 100, settings) == 100
    with pytest.raises(LimitExceededError):
        enforce_payload_size("x" * 101, settings)

    enforce_batch_size(2, settings)
    with pytest.raises(LimitExceededError):
        enforce_batch_size(3, settings)


def test_service_rejects_oversized_payload(
    session_factory: SessionFactory, settings: Settings
) -> None:
    tiny = settings.model_copy(update={"max_payload_bytes": 64})
    service = IngestionService(session_factory, tiny)

    result = service.ingest_event(_event({"blob": "x" * 500}), request_id="req-limits")

    assert result.status == "rejected"
    assert result.error is not None
    assert result.error.code == ErrorCode.PAYLOAD_TOO_LARGE.value


def test_service_rejects_oversized_batch(
    session_factory: SessionFactory, settings: Settings
) -> None:
    tiny = settings.model_copy(update={"max_batch_size": 1})
    service = IngestionService(session_factory, tiny)

    with pytest.raises(LimitExceededError):
        service.ingest_event_batch([_event({"n": 1}), _event({"n": 2})], request_id="req-batch")


def test_payload_at_the_exact_boundary_is_accepted(
    session_factory: SessionFactory, settings: Settings
) -> None:
    document = _event({"note": "boundary"})
    measured = len(canonical_json(document))

    at_limit = settings.model_copy(update={"max_payload_bytes": measured})
    accepted = IngestionService(session_factory, at_limit).ingest_event(
        document, request_id="req-at-limit"
    )
    assert accepted.status == "created"

    below = settings.model_copy(update={"max_payload_bytes": measured - 1})
    rejected = IngestionService(session_factory, below).ingest_event(
        document, request_id="req-below-limit"
    )
    assert rejected.status == "rejected"
    assert rejected.error is not None
    assert rejected.error.code == ErrorCode.PAYLOAD_TOO_LARGE.value
