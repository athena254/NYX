"""The checked-in Alembic migration must build a fresh database."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {
    "alembic_version",
    "audit_entries",
    "claims",
    "events",
    "evidence_items",
    "monitored_systems",
}


def test_alembic_upgrade_head_creates_expected_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.sqlite3"
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert database_path.exists()

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        revisions = [
            row[0] for row in connection.execute("SELECT version_num FROM alembic_version")
        ]

    assert tables >= EXPECTED_TABLES
    assert len(revisions) == 1
