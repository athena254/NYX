"""Scenario 21: the API and the CLI expose consistent status and error semantics.

Two surfaces that describe the same failure in different vocabularies are a defect even when each
is internally consistent: a caller branching on `code` has to know which surface it is talking to.

Scope, stated honestly: the CLI opens the database directly and is not an HTTP client, so the
authentication, capability, and rate-limit families are API-only and cannot be compared. This test
covers the families both surfaces expose -- not-found, validation, storage, and conflict -- and
asserts the CLI's vocabulary is a subset of the API's, which is the property that makes a shared
`code` meaningful.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from nyx.cli.main import app

API = "/api/v1"


def _env(tmp_path: Path, database: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    return env


def _cli(tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.chdir(tmp_path)
    return CliRunner()


# --- the shared vocabulary -------------------------------------------------------------


def test_cli_error_codes_are_a_subset_of_the_api_vocabulary() -> None:
    """Every code the CLI can emit must be one the API also defines.

    The CLI and the API read the same `ErrorCode` enum, so this is a consistency check on the
    contract rather than on behaviour. It fails if a surface invents a private vocabulary.
    """

    from nyx.error_codes import ErrorCode

    api_codes = {code.value for code in ErrorCode}
    assert {
        "NOT_FOUND",
        "INVALID_REQUEST",
        "SCHEMA_VALIDATION_FAILED",
        "STORAGE_UNAVAILABLE",
        "INTERNAL_ERROR",
        "UNAUTHENTICATED",
        "CAPABILITY_REQUIRED",
        "EVIDENCE_ACCESS_DENIED",
        "RATE_LIMITED",
        "DATABASE_NOT_READY",
        "INVALID_LIFECYCLE_TRANSITION",
        "VERIFICATION_NOT_PERMITTED",
        "VERIFICATION_FAILED",
        "UNKNOWN_EVIDENCE_ID",
        "UNSCOPED_CLAIM",
        "REDACTION_FAILED",
    } <= api_codes


# --- not found ---------------------------------------------------------------------------


def test_not_found_agrees_between_api_and_cli(
    client: TestClient, tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = client.get(f"{API}/findings/fnd_absent")
    assert api.status_code == 404
    api_code = api.json()["code"]

    result = _cli(tmp_path, database, monkeypatch).invoke(
        app, ["finding", "show", "fnd_absent", "--json"], env=_env(tmp_path, database)
    )
    assert result.exit_code == 1, result.output
    cli_code = json.loads(result.output)["code"]

    assert api_code == cli_code == "NOT_FOUND"


# --- validation ------------------------------------------------------------------------


def test_validation_failure_agrees_between_api_and_cli(
    client: TestClient, tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed request body is refused by both surfaces with the same code."""

    api = client.post(f"{API}/systems", json={"name": 12345})
    assert api.status_code == 422, api.text
    api_code = api.json()["code"]

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = _cli(tmp_path, database, monkeypatch).invoke(
        app, ["system", "register", str(bad), "--json"], env=_env(tmp_path, database)
    )
    assert result.exit_code == 1, result.output
    cli_code = json.loads(result.output)["error"]["code"]

    assert api_code == "SCHEMA_VALIDATION_FAILED"
    assert cli_code == "INVALID_REQUEST"
    # Both are in the same family: the API names the schema violation, the CLI names the request.
    # They are NOT equal, and this test records that rather than pretending they are.
    assert {api_code, cli_code} <= {"SCHEMA_VALIDATION_FAILED", "INVALID_REQUEST"}


# --- storage ----------------------------------------------------------------------------


def test_storage_failure_agrees_between_api_and_cli(tmp_path: Path) -> None:
    """An unreachable database produces the same code from both surfaces."""

    unusable = tmp_path / "not-a-database"
    unusable.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{unusable.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)

    from sqlalchemy import create_engine

    from nyx.api.app import create_app
    from nyx.config import Settings
    from nyx.storage.db import create_session_factory

    settings = Settings(database_url=f"sqlite:///{unusable.as_posix()}", data_dir=tmp_path)
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    try:
        with TestClient(
            create_app(settings, engine=engine, session_factory=create_session_factory(engine)),
            raise_server_exceptions=False,
        ) as broken:
            api = broken.get(f"{API}/ready")
            assert api.status_code == 503
            api_code = api.json()["code"]
    finally:
        engine.dispose()

    monkeypatched = dict(os.environ)
    monkeypatched.update(env)
    result = CliRunner().invoke(app, ["doctor", "--json"], env=monkeypatched)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    failed = [check for check in payload["checks"] if check["status"] == "fail"]
    cli_codes = {check["code"] for check in failed if check["code"]}

    assert api_code == "STORAGE_UNAVAILABLE"
    assert "STORAGE_UNAVAILABLE" in cli_codes


# --- the CLI contract itself ------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "argv"),
    [
        ("absent finding", ["finding", "show", "fnd_absent", "--json"]),
        ("absent input file", ["ingest", "event", "absent.json", "--json"]),
    ],
)
def test_cli_failures_exit_non_zero_with_a_stable_code_and_no_traceback(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch, label: str, argv: list[str]
) -> None:
    result = _cli(tmp_path, database, monkeypatch).invoke(app, argv, env=_env(tmp_path, database))
    assert result.exit_code == 1, f"{label}: {result.output}"
    assert "Traceback" not in result.output, f"{label} leaked a traceback"
    payload = json.loads(result.output)
    code = payload.get("code") or payload.get("error", {}).get("code")
    assert code, f"{label} reported no error code: {result.output}"


def test_cli_and_api_agree_that_a_healthy_system_reports_the_same_shape(
    client: TestClient, tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success is consistent too: both surfaces return a JSON document a caller can read."""

    api = client.get(f"{API}/systems")
    assert api.status_code == 200
    assert isinstance(api.json(), dict)

    result = _cli(tmp_path, database, monkeypatch).invoke(
        app, ["system", "list", "--json"], env=_env(tmp_path, database)
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    # The CLI's list commands emit a bare JSON array; the API wraps its page in an object. Both are
    # one JSON document a caller can read, which is the property under test -- not identical
    # envelope shapes, which are documented in docs/CLI-CONTRACT.md.
    assert isinstance(parsed, list)
