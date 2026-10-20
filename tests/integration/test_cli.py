"""CLI ingestion surface (SPEC §9.2): real files, non-zero exits, machine-readable output."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from nyx.cli.main import app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _env(tmp_path: Path, database: Path) -> dict[str, str]:
    """Environment for a CLI invocation, with NYX settings under the test's control.

    The OS environment is stripped of ``NYX_*`` so a developer's real settings cannot leak in.
    The ``.env`` *file* is handled by the caller: pydantic-settings reads it relative to the
    working directory, so the CLI tests chdir to an empty directory (see ``test_cli_*``).
    """

    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    return env


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_cli_registers_lists_and_ingests(tmp_path: Path, database: Path) -> None:
    runner = CliRunner()
    env = _env(tmp_path, database)

    registered = runner.invoke(
        app, ["system", "register", str(FIXTURES / "system_manifest.json")], env=env
    )
    assert registered.exit_code == 0, registered.output
    assert "created sys_ghost_factory_demo" in registered.output

    listed = runner.invoke(app, ["system", "list", "--json"], env=env)
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)[0]["id"] == "sys_ghost_factory_demo"

    event_path = FIXTURES / "event_completion_claim.json"
    ingested = runner.invoke(app, ["ingest", "event", str(event_path)], env=env)
    assert ingested.exit_code == 0, ingested.output
    assert "created" in ingested.output

    replay = runner.invoke(app, ["ingest", "event", str(event_path)], env=env)
    assert replay.exit_code == 0, replay.output
    assert "duplicate" in replay.output

    claim = runner.invoke(
        app, ["claim", "add", str(FIXTURES / "claim_completion_abc123.json")], env=env
    )
    assert claim.exit_code == 0, claim.output

    evidence = runner.invoke(
        app, ["evidence", "add", str(FIXTURES / "evidence_test_def456.json")], env=env
    )
    assert evidence.exit_code == 0, evidence.output


def test_cli_rejects_invalid_event_with_non_zero_exit(tmp_path: Path, database: Path) -> None:
    runner = CliRunner()
    env = _env(tmp_path, database)
    invalid = _fixture("event_completion_claim.json")
    del invalid["event_type"]
    path = tmp_path / "invalid-event.json"
    path.write_text(json.dumps(invalid), encoding="utf-8")

    result = runner.invoke(app, ["ingest", "event", str(path)], env=env)

    assert result.exit_code == 1
    assert "SCHEMA_VALIDATION_FAILED" in result.output


def test_cli_batch_exits_non_zero_when_any_item_is_rejected(tmp_path: Path, database: Path) -> None:
    runner = CliRunner()
    env = _env(tmp_path, database)
    good = _fixture("event_completion_claim.json")
    good["producer_event_id"] = "cli-batch-good"
    bad = _fixture("event_completion_claim.json")
    bad.pop("id")
    bad.pop("event_type")
    bad["producer_event_id"] = "cli-batch-bad"
    path = tmp_path / "batch.json"
    path.write_text(json.dumps({"events": [good, bad]}), encoding="utf-8")

    result = runner.invoke(app, ["ingest", "batch", str(path), "--json"], env=env)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["results"][0]["status"] == "created"
    assert payload["results"][1]["status"] == "rejected"
    assert payload["results"][1]["reason"] == "SCHEMA_VALIDATION_FAILED"


def test_cli_doctor_passes_on_a_migrated_database(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    env = _env(tmp_path, database)
    # pydantic-settings reads `.env` relative to the working directory; run from an empty one so
    # the developer's own NYX_* settings cannot turn "no optional integrations" into a failure.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["doctor", "--json"], env=env)

    assert result.exit_code == 0, result.output
    checks = json.loads(result.output)["checks"]
    assert all(check["status"] == "ok" for check in checks)
