"""Shared test fixtures.

Integration tests run against a real SQLite database built by the checked-in Alembic migration,
not by ``Base.metadata.create_all``. A session-scoped template database is migrated once and
copied per test so tests are isolated and still exercise the migration and the ``alembic_version``
table that readiness depends on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from nyx.api.app import create_app
from nyx.config import Settings
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory, create_engine_from_settings, create_session_factory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def migration_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a migrated template database once for the whole test session."""

    directory = tmp_path_factory.mktemp("nyx-migration-template")
    database = directory / "template.sqlite3"
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(directory)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return database


@pytest.fixture()
def database(tmp_path: Path, migration_template: Path) -> Path:
    """Return a fresh, isolated copy of the migrated database."""

    target = tmp_path / "nyx.sqlite3"
    shutil.copy(migration_template, target)
    return target


@pytest.fixture()
def settings(tmp_path: Path, database: Path) -> Settings:
    return Settings(database_url=f"sqlite:///{database.as_posix()}", data_dir=tmp_path)


@pytest.fixture()
def engine(settings: Settings) -> Iterator[Engine]:
    created = create_engine_from_settings(settings)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture()
def session_factory(engine: Engine) -> SessionFactory:
    return create_session_factory(engine)


@pytest.fixture()
def service(session_factory: SessionFactory, settings: Settings) -> IngestionService:
    return IngestionService(session_factory, settings)


@pytest.fixture()
def client(
    settings: Settings, engine: Engine, session_factory: SessionFactory
) -> Iterator[TestClient]:
    app = create_app(settings, engine=engine, session_factory=session_factory)
    with TestClient(app) as test_client:
        yield test_client
