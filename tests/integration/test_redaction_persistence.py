"""Scenario 14: redaction happens before persistence; secrets never reach the database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nyx.ingestion.redaction import PLACEHOLDER_PREFIX
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.audit import AuditRepository
from nyx.storage.repositories.events import EventRepository

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

_BEARER = "Bearer abcdefghijklmnop123456"
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _event_with_secrets() -> dict[str, Any]:
    document = json.loads((FIXTURES / "event_completion_claim.json").read_text(encoding="utf-8"))
    document.pop("id", None)
    document["producer_event_id"] = "redaction-1"
    document["payload"] = {
        "statement": "All required tests pass for commit abc123.",
        "operator_note": f"used {_BEARER} to push",
        "deploy_key": _AWS_KEY,
        "credentials": {"password": "hunter2"},
    }
    return document


def test_secrets_are_redacted_before_persistence(
    service: IngestionService, session_factory: SessionFactory, settings: Any
) -> None:
    result = service.ingest_event(_event_with_secrets(), request_id="req-redact")
    assert result.status == "created"

    with session_scope(session_factory) as session:
        stored = EventRepository(session).get(result.record_id or "")
    assert stored is not None
    rendered = json.dumps(stored.payload)

    assert _BEARER not in rendered
    assert _AWS_KEY not in rendered
    assert "hunter2" not in rendered
    assert PLACEHOLDER_PREFIX in rendered

    database_file = settings.database_url.removeprefix("sqlite:///")
    raw = Path(database_file).read_bytes()
    for secret in (_BEARER, _AWS_KEY, b"hunter2"):
        needle = secret.encode("utf-8") if isinstance(secret, str) else secret
        assert needle not in raw, "a secret was written to the database file"


def test_audit_records_the_redaction_rationale(
    service: IngestionService, session_factory: SessionFactory
) -> None:
    result = service.ingest_event(_event_with_secrets(), request_id="req-redact-audit")
    assert result.status == "created"

    with session_scope(session_factory) as session:
        entries = AuditRepository(session).list()
    assert entries
    reasons = " ".join(entry.reason or "" for entry in entries)
    assert "redaction rules fired" in reasons
    assert _BEARER not in reasons
    assert "hunter2" not in reasons


def test_clean_payload_audit_says_no_redaction(
    service: IngestionService, session_factory: SessionFactory
) -> None:
    document = json.loads((FIXTURES / "event_completion_claim.json").read_text(encoding="utf-8"))
    document.pop("id", None)
    document["producer_event_id"] = "redaction-clean"

    assert service.ingest_event(document, request_id="req-clean").status == "created"

    with session_scope(session_factory) as session:
        entries = AuditRepository(session).list()
    assert any("no redaction rules fired" in (entry.reason or "") for entry in entries)
