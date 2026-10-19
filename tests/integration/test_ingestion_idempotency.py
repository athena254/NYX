"""Scenario 2 and SPEC §11.2: identity-based idempotency, never content-similarity merging."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.events import EventRepository

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _event(producer_event_id: str) -> dict[str, Any]:
    document = json.loads((FIXTURES / "event_completion_claim.json").read_text(encoding="utf-8"))
    document.pop("id", None)
    document["producer_event_id"] = producer_event_id
    return document


def test_identical_replay_is_idempotent(
    service: IngestionService, session_factory: SessionFactory
) -> None:
    document = _event("producer-replay-1")

    first = service.ingest_event(document, request_id="req-1")
    second = service.ingest_event(document, request_id="req-2")

    assert first.status == "created"
    assert second.status == "duplicate"
    assert second.record_id == first.record_id
    assert second.error is not None
    assert second.error.code == "DUPLICATE_EVENT"

    with session_scope(session_factory) as session:
        assert len(EventRepository(session).list()) == 1


def test_different_producer_id_with_identical_content_creates_two_records(
    service: IngestionService, session_factory: SessionFactory
) -> None:
    left = _event("producer-incident-a")
    right = _event("producer-incident-b")

    first = service.ingest_event(left, request_id="req-a")
    second = service.ingest_event(right, request_id="req-b")

    assert first.status == "created"
    assert second.status == "created"
    assert first.record_id != second.record_id

    with session_scope(session_factory) as session:
        rows = EventRepository(session).list()
        assert len(rows) == 2
        assert {row.id for row in rows} == {first.record_id, second.record_id}


def test_same_key_with_a_different_payload_is_a_conflict(
    service: IngestionService,
) -> None:
    first = _event("shared-key")
    first["payload"] = {"statement": "first"}
    second = _event("shared-key")
    second["payload"] = {"statement": "second"}

    created = service.ingest_event(first, request_id="req-1")
    conflicting = service.ingest_event(second, request_id="req-2")

    assert created.status == "created"
    assert conflicting.status == "rejected"
    assert conflicting.error is not None
    assert conflicting.error.code == "IDEMPOTENCY_KEY_REUSED"


def test_evidence_and_claim_deduplication(
    service: IngestionService, session_factory: SessionFactory
) -> None:
    evidence = json.loads((FIXTURES / "evidence_test_def456.json").read_text(encoding="utf-8"))
    evidence.pop("id", None)

    first = service.register_evidence(evidence, request_id="req-e1")
    second = service.register_evidence(evidence, request_id="req-e2")

    assert first.status == "created"
    assert second.status == "duplicate"
    assert second.record_id == first.record_id
