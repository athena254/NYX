"""Retention purge and redaction against a migrated database (SPEC §11.3).

These tests assert on the **stored rows**, not on the return values of the services. A purge that
"worked" but wrote nothing, or that rewrote a row it was never allowed to touch, is exactly the
failure this file exists to catch.

The append-only assertion is the point of the file: findings, rebuttals, and dispositions must be
**byte-identical** before and after a purge. "No exception was raised" is not evidence that a row
was not rewritten.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nyx.domain.base import PrincipalRef
from nyx.domain.enums import PrincipalKind, RetentionClass
from nyx.ingestion.service import IngestionService
from nyx.retention.policy import RetentionPolicy
from nyx.retention.redaction import RedactionDeniedError, RedactionService
from nyx.retention.service import RetentionService
from nyx.retention.tombstone import TOMBSTONE_PREFIX
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.audit import AuditRepository

_NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
_DUE_AT = _NOW - timedelta(days=8)
_ACTOR = PrincipalRef(id="principal_operator", kind=PrincipalKind.HUMAN)

_APPEND_ONLY = ("findings", "rebuttals", "dispositions")


class _AllowAll:
    def may_redact(self, actor: PrincipalRef, target_type: str, action: str) -> bool:
        return True


class _DenyAll:
    def may_redact(self, actor: PrincipalRef, target_type: str, action: str) -> bool:
        return False


# -- helpers --------------------------------------------------------------------


def _database_path(settings: Any) -> str:
    return settings.database_url.removeprefix("sqlite:///")


# Fixed queries keyed by table name: the table is a constant chosen by the test, never by a
# caller or a payload, and interpolating it into SQL would be an injection surface for nothing.
_TABLE_QUERIES = {
    "audit_entries": "SELECT * FROM audit_entries ORDER BY id",
    "dispositions": "SELECT * FROM dispositions ORDER BY id",
    "events": "SELECT * FROM events ORDER BY id",
    "findings": "SELECT * FROM findings ORDER BY id",
    "rebuttals": "SELECT * FROM rebuttals ORDER BY id",
}


def _raw_rows(database_path: str, table: str) -> list[tuple]:
    with sqlite3.connect(database_path) as connection:
        return list(connection.execute(_TABLE_QUERIES[table]))


def _file_digest(database_path: str) -> str:
    with open(database_path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _seed_event(
    service: IngestionService,
    *,
    event_id: str,
    occurred_at: datetime,
    retention_class: RetentionClass,
    payload: dict[str, Any],
) -> None:
    document = {
        "id": event_id,
        "source_system_id": "sys_retention_demo",
        "event_type": "completion.claim.submitted",
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "correlation_id": f"corr-{event_id}",
        "payload": payload,
        "source_ref": f"retention://{event_id}",
        "trust_class": "observed",
        "labels": ["retention"],
        "retention_class": retention_class.value,
    }
    result = service.ingest_event(document, request_id=f"req-{event_id}")
    assert result.status == "created", result.error


def _seed_append_only(database_path: str) -> None:
    """Insert one row into each append-only table, with values a rewrite would change."""

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO findings (id, schema_version, title, summary, category, severity, "
            "scope, scope_system_id, affected_system_ids, claim_ids, event_ids, evidence_ids, "
            "supporting_evidence_ids, counterevidence_ids, reasoning, limitations, unknowns, "
            "recommended_action, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "fnd_retention_1",
                "1.0",
                "A finding that must survive a purge",
                "The summary must not change.",
                "missing_verification",
                "medium",
                json.dumps({"system_id": "sys_retention_demo", "commit": "abc123"}),
                "sys_retention_demo",
                json.dumps(["sys_retention_demo"]),
                json.dumps([]),
                json.dumps([]),
                json.dumps(["ev_retention_due"]),
                json.dumps(["ev_retention_due"]),
                json.dumps([]),
                json.dumps({"observations": [], "inferences": [], "confidence": "low"}),
                json.dumps(["Synthetic finding."]),
                json.dumps([]),
                json.dumps({"action": "none"}),
                "detected",
                "2026-10-21 12:00:00+00:00",
                "2026-10-21 12:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO rebuttals (id, schema_version, finding_id, submitted_by, submitted_at, "
            "argument, evidence_ids, requested_outcome) VALUES (?,?,?,?,?,?,?,?)",
            (
                "rbt_retention_1",
                "1.0",
                "fnd_retention_1",
                "sys_retention_demo",
                "2026-10-21 12:00:00+00:00",
                "The evidence this relies on has since been redacted.",
                json.dumps(["ev_retention_due"]),
                "withdrawn",
            ),
        )
        connection.execute(
            "INSERT INTO dispositions (id, schema_version, finding_id, outcome, rationale, "
            "decided_by, decided_at, verification_run_ids, policy_version, "
            "supersedes_disposition_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "dsp_retention_1",
                "1.0",
                "fnd_retention_1",
                "inconclusive",
                "The evidence this disposition relied on was redacted and remains contestable.",
                "principal_operator",
                "2026-10-21 12:00:00+00:00",
                json.dumps([]),
                "1.0",
                None,
            ),
        )
        connection.commit()


def _audit_entries(session_factory: SessionFactory, target_id: str) -> list:
    with session_scope(session_factory) as session:
        return AuditRepository(session).list(target_id=target_id)


# -- the purge ------------------------------------------------------------------


def test_a_due_record_is_tombstoned_and_audited(
    service: IngestionService, session_factory: SessionFactory, settings: Any
) -> None:
    _seed_event(
        service,
        event_id="evt_retention_due",
        occurred_at=_DUE_AT,
        retention_class=RetentionClass.EPHEMERAL,
        payload={"statement": "Ephemeral payload that must be tombstoned."},
    )
    database_path = _database_path(settings)
    assert "Ephemeral payload that must be tombstoned." in str(_raw_rows(database_path, "events"))

    with session_scope(session_factory) as session:
        report = RetentionService(
            session, policy=RetentionPolicy(7, 90, 365, 730), actor=_ACTOR
        ).purge_due(now=_NOW, dry_run=False)

    assert report.purged == 1
    assert report.skipped_legal_hold == 0
    assert report.dry_run is False

    stored = str(_raw_rows(database_path, "events"))
    assert TOMBSTONE_PREFIX in stored
    assert "Ephemeral payload that must be tombstoned." not in stored

    entries = _audit_entries(session_factory, "evt_retention_due")
    assert entries, "the redaction was not recorded in the audit log"
    assert "Ephemeral payload" not in entries[0].reason


def test_a_legal_hold_record_survives_a_purge(
    service: IngestionService, session_factory: SessionFactory, settings: Any
) -> None:
    _seed_event(
        service,
        event_id="evt_retention_hold",
        occurred_at=_NOW - timedelta(days=4000),
        retention_class=RetentionClass.LEGAL_HOLD,
        payload={"statement": "Held payload that must never be purged."},
    )
    database_path = _database_path(settings)

    with session_scope(session_factory) as session:
        report = RetentionService(
            session, policy=RetentionPolicy(7, 90, 365, 730), actor=_ACTOR
        ).purge_due(now=_NOW + timedelta(days=3650), dry_run=False)

    assert report.skipped_legal_hold == 1
    assert report.purged == 0

    stored = str(_raw_rows(database_path, "events"))
    assert "Held payload that must never be purged." in stored
    assert TOMBSTONE_PREFIX not in stored


def test_dry_run_changes_nothing(
    service: IngestionService, session_factory: SessionFactory, settings: Any
) -> None:
    _seed_event(
        service,
        event_id="evt_retention_dry",
        occurred_at=_DUE_AT,
        retention_class=RetentionClass.EPHEMERAL,
        payload={"statement": "Payload that a dry run must leave alone."},
    )
    _seed_append_only(_database_path(settings))
    database_path = _database_path(settings)

    before = {table: _raw_rows(database_path, table) for table in ("events", *_APPEND_ONLY)}
    audit_before = _raw_rows(database_path, "audit_entries")
    digest_before = _file_digest(database_path)

    with session_scope(session_factory) as session:
        report = RetentionService(
            session, policy=RetentionPolicy(7, 90, 365, 730), actor=_ACTOR
        ).purge_due(now=_NOW, dry_run=True)

    # The report says what *would* have happened; the store says nothing did.
    assert report.purged == 1
    assert report.dry_run is True

    after = {table: _raw_rows(database_path, table) for table in ("events", *_APPEND_ONLY)}
    assert after == before, "a dry run changed a stored row"
    assert _raw_rows(database_path, "audit_entries") == audit_before
    assert _file_digest(database_path) == digest_before


def test_append_only_rows_are_byte_identical_before_and_after_a_purge(
    service: IngestionService, session_factory: SessionFactory, settings: Any
) -> None:
    """Asserted directly on the stored rows -- not by the absence of an exception."""

    _seed_event(
        service,
        event_id="evt_retention_due",
        occurred_at=_DUE_AT,
        retention_class=RetentionClass.EPHEMERAL,
        payload={"statement": "Due payload."},
    )
    _seed_append_only(_database_path(settings))
    database_path = _database_path(settings)

    before = {table: _raw_rows(database_path, table) for table in _APPEND_ONLY}
    assert before["findings"], "the append-only fixture did not insert a finding"

    with session_scope(session_factory) as session:
        RetentionService(session, policy=RetentionPolicy(7, 90, 365, 730), actor=_ACTOR).purge_due(
            now=_NOW, dry_run=False
        )

    after = {table: _raw_rows(database_path, table) for table in _APPEND_ONLY}
    assert after == before, "the purge rewrote an append-only row"
    assert after["findings"] == before["findings"]
    assert after["rebuttals"] == before["rebuttals"]
    assert after["dispositions"] == before["dispositions"]


def test_the_due_event_row_keeps_its_identity_and_relationships(
    service: IngestionService, session_factory: SessionFactory, settings: Any
) -> None:
    """Only the content goes; the record itself stays inspectable."""

    _seed_event(
        service,
        event_id="evt_retention_due",
        occurred_at=_DUE_AT,
        retention_class=RetentionClass.EPHEMERAL,
        payload={"statement": "Due payload.", "repository": "example/demo"},
    )
    database_path = _database_path(settings)
    before = next(
        row for row in _raw_rows(database_path, "events") if row[0] == "evt_retention_due"
    )

    with session_scope(session_factory) as session:
        RetentionService(session, policy=RetentionPolicy(7, 90, 365, 730), actor=_ACTOR).purge_due(
            now=_NOW, dry_run=False
        )

    after = next(row for row in _raw_rows(database_path, "events") if row[0] == "evt_retention_due")
    assert before[0] == after[0]  # identity
    assert before[4] == after[4]  # occurred_at
    assert before[6] == after[6]  # correlation_id
    assert before[8] == after[8]  # parent_event_id
    assert TOMBSTONE_PREFIX in str(after)
    assert "Due payload." not in str(after)


# -- redaction ------------------------------------------------------------------


def test_redacting_a_legal_hold_record_is_denied(
    service: IngestionService, session_factory: SessionFactory, settings: Any
) -> None:
    _seed_event(
        service,
        event_id="evt_retention_hold",
        occurred_at=_NOW - timedelta(days=4000),
        retention_class=RetentionClass.LEGAL_HOLD,
        payload={"statement": "Held payload that must not be redacted."},
    )
    database_path = _database_path(settings)
    before = _raw_rows(database_path, "events")

    with (
        session_scope(session_factory) as session,
        pytest.raises(RedactionDeniedError) as excinfo,
    ):
        RedactionService(session, actor=_ACTOR, allowlist=_AllowAll()).redact(
            target_type="event",
            target_id="evt_retention_hold",
            action="payload_redacted",
            reason="operator requested removal",
        )

    assert "legal_hold" in excinfo.value.reason
    assert "release the hold" in excinfo.value.reason
    assert _raw_rows(database_path, "events") == before, "a denied redaction changed a row"

    entries = _audit_entries(session_factory, "evt_retention_hold")
    denials = [entry for entry in entries if entry.outcome.value == "denied"]
    assert denials, "the denial was not recorded"
    assert "legal_hold" in denials[0].reason


def test_redacting_a_normal_record_tombstones_it_and_writes_an_audit_entry(
    service: IngestionService, session_factory: SessionFactory, settings: Any
) -> None:
    _seed_event(
        service,
        event_id="evt_retention_redact",
        occurred_at=_NOW,
        retention_class=RetentionClass.STANDARD,
        payload={"statement": "Payload to be redacted on request."},
    )
    database_path = _database_path(settings)

    with session_scope(session_factory) as session:
        outcome = RedactionService(session, actor=_ACTOR, allowlist=_AllowAll()).redact(
            target_type="event",
            target_id="evt_retention_redact",
            action="payload_redacted",
            reason="data-subject request",
        )

    assert outcome.record_id == "evt_retention_redact"
    assert outcome.action == "payload_redacted"
    assert outcome.fields_affected
    assert outcome.audit_entry_id

    stored = str(_raw_rows(database_path, "events"))
    assert "Payload to be redacted on request." not in stored
    assert TOMBSTONE_PREFIX in stored
    assert _ACTOR.id in stored


def test_redaction_is_denied_without_the_retention_capability(
    service: IngestionService, session_factory: SessionFactory, settings: Any
) -> None:
    _seed_event(
        service,
        event_id="evt_retention_nocap",
        occurred_at=_NOW,
        retention_class=RetentionClass.STANDARD,
        payload={"statement": "Payload that must survive an unauthorised request."},
    )
    database_path = _database_path(settings)
    before = _raw_rows(database_path, "events")

    with session_scope(session_factory) as session, pytest.raises(RedactionDeniedError):
        RedactionService(session, actor=_ACTOR, allowlist=_DenyAll()).redact(
            target_type="event",
            target_id="evt_retention_nocap",
            action="payload_redacted",
            reason="unauthorised request",
        )

    assert _raw_rows(database_path, "events") == before
