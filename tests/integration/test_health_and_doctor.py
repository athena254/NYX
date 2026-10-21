"""Scenario 20: `nyx doctor`, health, and readiness report dependency state without leaking secrets.

Registering a route is not the same as the route working. These tests drive the endpoints and the
CLI against a real migrated database and read what comes back.

The secret-safety test is the one that matters most: a diagnostic command that prints its effective
configuration is exactly where a credential leaks, and a test that only checks "the command ran"
would not notice.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from typer.testing import CliRunner

from nyx.api.app import create_app
from nyx.cli.main import app
from nyx.config import Settings
from nyx.storage.db import create_session_factory

# Values that must never appear in doctor output. Long enough that a truncation cannot make the
# assertion vacuous, and not substrings of anything else in the output.
_SECRET_VALUES = {
    "NYX_AUTH_API_KEY": "sk-doctor-probe-A1b2C3d4E5f6",
    "NYX_LLM_API_KEY": "sk-doctor-probe-Z9y8X7w6V5u4",
    "NYX_TELEGRAM_BOT_TOKEN": "999999999:AA-doctor-probe-token-value",
}


def _event() -> dict[str, Any]:
    return {
        "source_system_id": "sys_health_probe",
        "event_type": "agent.completion_claim",
        "occurred_at": "2026-10-19T10:00:00Z",
        "correlation_id": "health-corr-1",
        "payload": {"note": "health probe"},
        "producer_event_id": "health-event-1",
        "trust_class": "observed",
        "labels": ["health-probe"],
        "retention_class": "standard",
    }


def test_health_and_ready_report_ok_against_a_migrated_database(
    client: TestClient,
) -> None:
    """Both endpoints report their dependency state honestly when everything is fine."""

    health = client.get("/api/v1/health")
    assert health.status_code == 200, health.text
    assert health.json()["status"] == "ok"

    ready = client.get("/api/v1/ready")
    assert ready.status_code == 200, ready.text
    body = ready.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"
    assert body["migrations"] == "at head"


def test_a_stopped_database_is_reported_not_crashed(tmp_path: Path) -> None:
    """A dependency failure is a status, not a traceback.

    `tests/integration/test_readiness_failure.py` proves the same property for ingestion; this
    asserts the endpoints themselves stay well-formed, which is the part a caller branches on.
    """

    unusable = tmp_path / "not-a-database"
    unusable.mkdir()
    settings = Settings(database_url=f"sqlite:///{unusable.as_posix()}", data_dir=tmp_path)
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    try:
        app = create_app(settings, engine=engine, session_factory=create_session_factory(engine))
        with TestClient(app, raise_server_exceptions=False) as broken:
            ready = broken.get("/api/v1/ready")
            assert ready.status_code == 503, ready.text
            assert ready.json()["code"] == "STORAGE_UNAVAILABLE"
            assert "Traceback" not in ready.text

            # Health is liveness, not readiness: the process is up, so it reports ok rather than
            # inheriting the dependency failure. That distinction is the point of having both.
            health = broken.get("/api/v1/health")
            assert health.status_code == 200, health.text
    finally:
        engine.dispose()


def test_health_and_ready_agree_on_a_reachable_database(
    client: TestClient,
) -> None:
    """The two surfaces must not disagree about a dependency that is present."""

    health = client.get("/api/v1/health").json()
    ready = client.get("/api/v1/ready").json()
    assert health["status"] == "ok"
    assert ready["status"] == "ready"
    assert ready["database"] == "ok"


def test_doctor_output_contains_no_secret(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diagnostic that prints effective configuration is where a credential leaks.

    Every secret-ish setting is set to a distinctive value, then the whole of doctor's output is
    searched for each of them. The redacted form is asserted to be present instead, so the test
    fails if a future change prints the raw value.
    """

    runner = CliRunner()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    env["NYX_AUTH_MODE"] = "api_key"
    env.update(_SECRET_VALUES)
    # pydantic-settings reads .env relative to the working directory; run from an empty one so the
    # developer's own settings cannot leak in or be leaked at.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["doctor", "--json"], env=env)
    assert result.exit_code == 0, result.output

    output = result.output
    parsed = json.loads(output)
    assert all(check["status"] == "ok" for check in parsed["checks"])

    for variable, value in _SECRET_VALUES.items():
        assert value not in output, f"{variable} leaked into doctor output"
        assert value[:8] not in output, f"a prefix of {variable} leaked into doctor output"
    assert "***redacted***" in output, "the redacted form should be what shows"


def test_doctor_exits_non_zero_and_never_raises_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed check is a non-zero exit and a structured message, not a crash."""

    runner = CliRunner()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NYX_")}
    # A database path that cannot be opened: the database check fails, and the command must say so
    # through its exit code.
    env["NYX_DATABASE_URL"] = f"sqlite:///{tmp_path.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["doctor", "--json"], env=env)
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    parsed = json.loads(result.output)
    assert any(check["status"] == "fail" for check in parsed["checks"])
    failed = [check for check in parsed["checks"] if check["status"] == "fail"]
    assert all(check["code"] for check in failed), "a failed check must carry a code"


def test_doctor_emits_exactly_one_json_document(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--json` stdout is one JSON document and nothing else -- the machine interface contract."""

    runner = CliRunner()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["doctor", "--json"], env=env)
    assert result.exit_code == 0, result.output
    json.loads(result.output)
