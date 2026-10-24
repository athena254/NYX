"""Scenario 15: unavailable DB or migrations behind head fails safely and is not ready."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from nyx.api.app import create_app
from nyx.config import Settings
from nyx.storage.db import create_session_factory

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[2] / "examples" / "ghost-factory"


def _event() -> dict[str, Any]:
    document = json.loads((FIXTURES / "event-completion-claim.json").read_text(encoding="utf-8"))
    document.pop("id", None)
    document["producer_event_id"] = "readiness-event-1"
    return document


def test_unreachable_database_is_503_and_ingestion_does_not_succeed(
    tmp_path: Path,
) -> None:
    unusable = tmp_path / "not-a-database"
    unusable.mkdir()
    settings = Settings(database_url=f"sqlite:///{unusable.as_posix()}", data_dir=tmp_path)
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    try:
        app = create_app(settings, engine=engine, session_factory=create_session_factory(engine))
        with TestClient(app) as client:
            ready = client.get("/api/v1/ready")
            assert ready.status_code == 503
            assert ready.json()["code"] == "STORAGE_UNAVAILABLE"

            ingest = client.post("/api/v1/events", json=_event())
            assert ingest.status_code == 503
            assert ingest.json()["code"] in {
                "STORAGE_UNAVAILABLE",
                "INTERNAL_ERROR",
            }
    finally:
        engine.dispose()


def test_migrations_behind_head_is_not_ready(
    database: Path, settings: Settings, tmp_path: Path
) -> None:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    # Downgrade to base, not -1: since the Phase 2a migration added the findings/critic_runs
    # tables and the evidence_items.scope column, a -1 downgrade still leaves the events and
    # idempotency tables in place, so event ingestion would legitimately succeed. The property
    # under test is that a database behind head is not ready and ingestion fails safely, which
    # requires a downgrade that actually removes the tables the write path needs.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    try:
        app = create_app(settings, engine=engine, session_factory=create_session_factory(engine))
        with TestClient(app) as client:
            ready = client.get("/api/v1/ready")
            assert ready.status_code == 503
            body = ready.json()
            assert body["code"] == "DATABASE_NOT_READY"
            assert "not at head" in body["message"]

            ingest = client.post("/api/v1/events", json=_event())
            assert ingest.status_code == 503
            assert ingest.json()["code"] in {
                "STORAGE_UNAVAILABLE",
                "INTERNAL_ERROR",
            }
    finally:
        engine.dispose()
