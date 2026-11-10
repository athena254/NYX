"""Real predecessor-schema data must survive upgrade; unsafe downgrade must refuse."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import Engine
from tests.security.test_v002_provenance import _payload

from nyx.audit.writer import AuditEntryInput, AuditWriter
from nyx.domain.base import PrincipalRef
from nyx.domain.enums import AuditOutcome
from nyx.domain.evidence import EvidenceItem
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.evidence import EvidenceRepository

ROOT = Path(__file__).resolve().parents[2]


def _migration(database: Path, direction: str, revision: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(database.parent)
    return subprocess.run(  # noqa: S603 - fixed Alembic commands and owned fixture database
        [sys.executable, "-m", "alembic", direction, revision],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def _audit(database: Path) -> list[tuple]:
    with sqlite3.connect(database) as sql:
        return sql.execute("SELECT * FROM audit_entries ORDER BY sequence").fetchall()


def test_legacy_claims_upgrade_readback_and_nonlossy_downgrade_boundary(
    database: Path,
    engine: Engine,
    session_factory: SessionFactory,
) -> None:
    payload = _payload()
    payload.pop("producer_event_id")
    # Trusted repository construction seeds the exact shape that the old gateway
    # accepted as canonical, without importing an old application over a new schema.
    legacy = EvidenceItem.model_validate(payload)
    with session_scope(session_factory) as session:
        EvidenceRepository(session).add(legacy)
        AuditWriter(session).append(
            AuditEntryInput(
                actor=PrincipalRef(id="legacy-collector", kind="service"),
                action="evidence.registered",
                target_type="evidence",
                target_id=legacy.id,
                request_id="legacy-before-upgrade",
                outcome=AuditOutcome.SUCCESS,
                reason="Legacy evidence was accepted before the trust-boundary upgrade.",
            )
        )
    engine.dispose()
    down = _migration(database, "downgrade", "c5e6f7a8b9d0")
    assert down.returncode == 0, down.stdout + down.stderr
    with sqlite3.connect(database) as sql:
        columns = {row[1] for row in sql.execute("PRAGMA table_info(evidence_items)")}
        assert "claimed_verification" not in columns
        old_identity, old_verification = sql.execute(
            "SELECT producer_identity,verification FROM evidence_items WHERE id=?", (legacy.id,)
        ).fetchone()
        assert json.loads(old_identity)["id"] == "nyx-verifier"
        assert json.loads(old_verification)["result"] == "verified"
    audit = _audit(database)
    upgraded = _migration(database, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    with session_scope(session_factory) as session:
        readback = EvidenceRepository(session).get(legacy.id)
    assert readback is not None
    assert readback.producer_identity is None and readback.verification is None
    assert readback.claimed_producer_identity == legacy.producer_identity
    assert readback.claimed_verification == legacy.verification
    assert _audit(database) == audit
    engine.dispose()
    refused = _migration(database, "downgrade", "c5e6f7a8b9d0")
    assert refused.returncode != 0, "downgrade silently discarded preserved producer claims"
    assert "preserved producer claims" in refused.stderr
    with session_scope(session_factory) as session:
        assert EvidenceRepository(session).get(legacy.id) == readback
    assert _audit(database) == audit
