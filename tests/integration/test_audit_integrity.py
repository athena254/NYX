"""Acceptance scenario 17: audit history detects accidental record mutation.

The chain is written by :class:`AuditWriter` and checked by :class:`AuditVerifier`. The tests
mutate the stored rows directly, which is what "accidental mutation" means here: the application
has no update or delete path for audit entries.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nyx.audit.verifier import (
    NON_CLAIM,
    AuditVerifier,
    VerificationReport,
    verify_on_startup,
)
from nyx.audit.writer import AuditEntryInput, AuditWriter
from nyx.cli.main import app
from nyx.domain.base import PrincipalRef
from nyx.domain.enums import AuditOutcome, PrincipalKind
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.audit import AuditRepository
from nyx.storage.tables import AuditRow

_ACTOR = PrincipalRef(id="principal_operator", kind=PrincipalKind.HUMAN)


def _entry(
    *,
    sequence: int,
    timestamp: datetime,
    action: str = "system.registered",
    reason: str = "seeded audit entry",
) -> AuditEntryInput:
    return AuditEntryInput(
        schema_version="1.0",
        actor=_ACTOR,
        action=action,
        target_type="monitored_system",
        target_id=f"sys_integrity_{sequence}",
        request_id=f"req-integrity-{sequence}",
        correlation_id=None,
        outcome=AuditOutcome.SUCCESS,
        reason=reason,
        timestamp=timestamp,
    )


def _seed(session_factory: SessionFactory, count: int = 3) -> list[str]:
    """Append ``count`` entries and return their ids in sequence order."""

    ids: list[str] = []
    with session_scope(session_factory) as session:
        writer = AuditWriter(session)
        for index in range(1, count + 1):
            entry = writer.append(
                _entry(
                    sequence=index,
                    timestamp=datetime(2026, 10, 21, 12, index, tzinfo=UTC),
                    action="system.registered",
                )
            )
            ids.append(entry.id)
    return ids


def _verify(session_factory: SessionFactory) -> VerificationReport:
    with session_scope(session_factory) as session:
        return AuditVerifier(AuditRepository(session)).verify()


def test_clean_chain_verifies(session_factory: SessionFactory) -> None:
    _seed(session_factory)

    report = _verify(session_factory)

    assert report.ok is True
    assert report.entries_checked == 3
    assert report.first_divergence is None


def test_mutating_one_rows_reason_is_detected(session_factory: SessionFactory) -> None:
    ids = _seed(session_factory)
    with session_scope(session_factory) as session:
        row = session.get(AuditRow, ids[1])
        assert row is not None
        row.reason = "edited after it was written"

    report = _verify(session_factory)

    assert report.ok is False
    assert report.first_divergence is not None
    assert report.first_divergence.sequence == 2
    assert report.first_divergence.reason == "recomputed_hash_mismatch"


def test_deleting_a_middle_row_is_detected(session_factory: SessionFactory) -> None:
    ids = _seed(session_factory)
    with session_scope(session_factory) as session:
        row = session.get(AuditRow, ids[1])
        assert row is not None
        session.delete(row)

    report = _verify(session_factory)

    assert report.ok is False
    assert report.first_divergence is not None
    assert report.first_divergence.sequence == 3
    assert report.first_divergence.reason == "sequence_gap"


def test_deleting_the_first_row_is_not_reported_as_genesis(session_factory: SessionFactory) -> None:
    ids = _seed(session_factory)
    with session_scope(session_factory) as session:
        row = session.get(AuditRow, ids[0])
        assert row is not None
        session.delete(row)

    report = _verify(session_factory)

    assert report.ok is False
    assert report.first_divergence is not None
    assert report.first_divergence.sequence == 2
    assert report.first_divergence.reason == "not_genesis"


def test_overwriting_an_entry_hash_is_detected(session_factory: SessionFactory) -> None:
    ids = _seed(session_factory)
    with session_scope(session_factory) as session:
        row = session.get(AuditRow, ids[1])
        assert row is not None
        row.entry_hash = "sha256:" + "f" * 64

    report = _verify(session_factory)

    assert report.ok is False
    assert report.first_divergence is not None
    assert report.first_divergence.sequence == 2
    assert report.first_divergence.reason == "recomputed_hash_mismatch"


def test_empty_store_verifies_trivially(session_factory: SessionFactory) -> None:
    report = _verify(session_factory)

    assert report.ok is True
    assert report.entries_checked == 0
    assert report.first_divergence is None


def test_non_claim_is_stated_in_the_report(session_factory: SessionFactory) -> None:
    """Rewriting the whole database and recomputing every hash is out of scope.

    The report says so rather than implying tamper-proof storage. This is a documented boundary,
    not a missing feature (SPEC §5.8, ADR 0003).
    """

    report = _verify(session_factory)

    assert report.non_claim == NON_CLAIM
    assert "does not defeat a privileged attacker" in report.non_claim
    assert "rewrite the whole database and recompute every hash" in report.non_claim
    assert "tamper-proof" not in report.non_claim


def test_startup_verification_warns_without_refusing_to_boot(
    session_factory: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    ids = _seed(session_factory)
    with session_scope(session_factory) as session:
        row = session.get(AuditRow, ids[0])
        assert row is not None
        row.reason = "edited before startup"

    logger = logging.getLogger("nyx.audit.startup-test")
    with caplog.at_level(logging.WARNING, logger="nyx.audit.startup-test"):
        report = verify_on_startup(session_factory, logger=logger)

    assert report.ok is False
    assert any("audit chain diverged" in record.message for record in caplog.records)


def _cli_env(tmp_path: Path, database: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    env["NYX_LLM_ENABLED"] = "false"
    return env


def test_cli_verify_exits_zero_on_a_clean_chain(
    session_factory: SessionFactory,
    database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(session_factory)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app, ["audit", "verify", "--json"], env=_cli_env(tmp_path, database)
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["entries_checked"] == 3
    assert payload["non_claim"] == NON_CLAIM


def test_cli_verify_exits_one_on_divergence_without_a_traceback(
    session_factory: SessionFactory,
    database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _seed(session_factory)
    with session_scope(session_factory) as session:
        row = session.get(AuditRow, ids[2])
        assert row is not None
        row.reason = "edited after it was written"
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app, ["audit", "verify", "--json"], env=_cli_env(tmp_path, database)
    )

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["first_divergence"]["sequence"] == 3
    assert payload["first_divergence"]["reason"] == "recomputed_hash_mismatch"
