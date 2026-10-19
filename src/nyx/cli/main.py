"""NYX command-line interface (SPEC §10, Phase 1 subset).

Every command supports ``--json`` for machine-readable output, exits non-zero on failure, and
never prints secrets: it prints structured error codes and safe messages only. The CLI calls
the same :class:`IngestionService` as the HTTP API; it does not write to the database directly.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from nyx.config import Settings
from nyx.domain.ids import new_request_id
from nyx.error_codes import ErrorCode
from nyx.ingestion.errors import IngestResult, StructuredError
from nyx.ingestion.limits import LimitExceededError
from nyx.ingestion.service import IngestionService
from nyx.storage.db import (
    create_engine_from_settings,
    create_session_factory,
)
from nyx.storage.migrations import inspect_migrations

app = typer.Typer(
    name="nyx",
    help="NYX evidence-backed oversight service.",
    add_completion=False,
    no_args_is_help=True,
)

system_app = typer.Typer(help="Register and list monitored systems.", no_args_is_help=True)
ingest_app = typer.Typer(help="Ingest events.", no_args_is_help=True)
claim_app = typer.Typer(help="Register claims.", no_args_is_help=True)
evidence_app = typer.Typer(help="Register evidence items.", no_args_is_help=True)

app.add_typer(system_app, name="system")
app.add_typer(ingest_app, name="ingest")
app.add_typer(claim_app, name="claim")
app.add_typer(evidence_app, name="evidence")


@app.callback()
def main() -> None:
    """NYX evidence-backed oversight service."""


# --------------------------------------------------------------------------- helpers


def _read_raw(path: str | None) -> bytes:
    if path is None or path == "-":
        return sys.stdin.buffer.read()
    return Path(path).read_bytes()


def _load_json(raw: bytes, json_output: bool) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _emit_error(
            StructuredError(
                code=ErrorCode.INVALID_REQUEST.value,
                message="Input is not valid UTF-8 JSON.",
                request_id="cli",
            ),
            json_output,
        )
        raise typer.Exit(code=1) from None


def _emit_error(error: StructuredError, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(error.model_dump(exclude_none=True), indent=2))
    else:
        typer.echo(f"{error.code}: {error.message}", err=True)
        typer.echo(f"request_id: {error.request_id}", err=True)


def _emit_result(result: IngestResult, json_output: bool) -> None:
    if json_output:
        payload: dict[str, Any] = {"status": result.status, "record_id": result.record_id}
        if result.record is not None:
            payload["record"] = result.record.model_dump(mode="json", by_alias=True)
        if result.error is not None:
            payload["error"] = result.error.model_dump(exclude_none=True)
        typer.echo(json.dumps(payload, indent=2))
        return
    if result.status == "created":
        typer.echo(f"created {result.record_id}")
    elif result.status == "duplicate":
        typer.echo(f"duplicate {result.record_id}")
    else:
        assert result.error is not None  # nosec B101 - invariant of a rejected result
        typer.echo(f"{result.error.code}: {result.error.message}", err=True)


def _finish(result: IngestResult, json_output: bool) -> None:
    _emit_result(result, json_output)
    if result.status == "rejected":
        raise typer.Exit(code=1)


def _service() -> IngestionService:
    settings = Settings()
    engine = create_engine_from_settings(settings)
    return IngestionService(create_session_factory(engine), settings)


def _print_record(record: Any, json_output: bool, human: str) -> None:
    if json_output:
        typer.echo(json.dumps(record.model_dump(mode="json", by_alias=True), indent=2))
    else:
        typer.echo(human)


# --------------------------------------------------------------------------- system


@system_app.command("register")
def system_register(
    manifest: str = typer.Argument(..., help="Path to a system manifest JSON file."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Register a monitored system from a manifest file."""

    raw = _read_raw(manifest)
    result = _service().register_system(raw, request_id=new_request_id())
    _finish(result, json_output)


@system_app.command("list")
def system_list(
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List registered systems in stable creation order."""

    systems = _service().list_systems_page(limit=limit)
    if json_output:
        typer.echo(
            json.dumps([item.model_dump(mode="json", by_alias=True) for item in systems], indent=2)
        )
        return
    if not systems:
        typer.echo("no systems registered")
        return
    for item in systems:
        typer.echo(f"{item.id}  {item.name}  {item.environment.value}  {item.status.value}")


# --------------------------------------------------------------------------- ingest


@ingest_app.command("event")
def ingest_event(
    source: str = typer.Argument("-", help="Path to an event JSON file, or '-' for stdin."),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Ingest a single event from a file or stdin."""

    raw = _read_raw(source)
    result = _service().ingest_event(
        raw, request_id=new_request_id(), idempotency_key=idempotency_key
    )
    _finish(result, json_output)


@ingest_app.command("batch")
def ingest_batch(
    source: str = typer.Argument(..., help="Path to a JSON array or {'events': [...]} file."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Ingest a batch of events; one bad item does not fail the batch."""

    raw = _read_raw(source)
    document = _load_json(raw, json_output)
    payloads = document.get("events") if isinstance(document, dict) else document
    if not isinstance(payloads, list):
        _emit_error(
            StructuredError(
                code=ErrorCode.INVALID_REQUEST.value,
                message="Batch input must be a JSON array or an object with an 'events' array.",
                request_id="cli",
            ),
            json_output,
        )
        raise typer.Exit(code=1)
    request_id = new_request_id()
    try:
        results = _service().ingest_event_batch(payloads, request_id=request_id)
    except LimitExceededError as exc:
        _emit_error(
            StructuredError(
                code=ErrorCode.BATCH_TOO_LARGE.value,
                message=f"Batch contains {exc.observed} items, exceeding the maximum {exc.limit}.",
                request_id=request_id,
            ),
            json_output,
        )
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "request_id": request_id,
                    "results": [
                        {
                            "index": index,
                            "status": result.status,
                            "record_id": result.record_id,
                            "reason": None if result.error is None else result.error.code,
                        }
                        for index, result in enumerate(results)
                    ],
                },
                indent=2,
            )
        )
    else:
        for index, result in enumerate(results):
            label = result.status
            detail = result.record_id or (result.error.code if result.error is not None else "")
            typer.echo(f"[{index}] {label} {detail}")
    if any(result.status == "rejected" for result in results):
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- claim / evidence


@claim_app.command("add")
def claim_add(
    source: str = typer.Argument("-", help="Path to a claim JSON file, or '-' for stdin."),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Register a claim from a file or stdin."""

    raw = _read_raw(source)
    result = _service().register_claim(
        raw, request_id=new_request_id(), idempotency_key=idempotency_key
    )
    _finish(result, json_output)


@evidence_app.command("add")
def evidence_add(
    source: str = typer.Argument("-", help="Path to an evidence JSON file, or '-' for stdin."),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Register an evidence item from a file or stdin."""

    raw = _read_raw(source)
    result = _service().register_evidence(
        raw, request_id=new_request_id(), idempotency_key=idempotency_key
    )
    _finish(result, json_output)


# --------------------------------------------------------------------------- doctor


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Check configuration, database, migrations, writable paths, API, and OpenAPI freshness."""

    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, code: str | None, message: str) -> None:
        checks.append(
            {
                "check": name,
                "status": "ok" if ok else "fail",
                "code": code,
                "message": message,
            }
        )

    try:
        settings = Settings()
    except ValidationError as exc:
        record("configuration", False, ErrorCode.INVALID_REQUEST.value, "invalid settings")
        locations = ", ".join(
            ".".join(str(part) for part in error.get("loc", ())) or "<root>"
            for error in exc.errors(include_input=False)
        )
        _finish_doctor(checks, json_output, extra=f"configuration errors at: {locations}")
        raise typer.Exit(code=1) from None
    except Exception as exc:  # noqa: BLE001
        record(
            "configuration",
            False,
            ErrorCode.INTERNAL_ERROR.value,
            f"could not load settings ({type(exc).__name__})",
        )
        _finish_doctor(checks, json_output)
        raise typer.Exit(code=1) from None

    backend = make_url(settings.database_url).get_backend_name()
    record(
        "configuration",
        True,
        None,
        f"host={settings.host} port={settings.port} database={backend} "
        f"redaction={settings.redaction_enabled} auth_mode={settings.auth_mode}",
    )

    record(
        "data_directory",
        _check_data_dir(settings.data_dir),
        ErrorCode.STORAGE_UNAVAILABLE.value,
        f"data directory {settings.data_dir} writable",
    )

    engine: Engine | None = None
    try:
        engine = create_engine_from_settings(settings)
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        record("database", True, None, "reachable")
    except Exception as exc:  # noqa: BLE001
        record(
            "database",
            False,
            ErrorCode.STORAGE_UNAVAILABLE.value,
            f"not reachable ({type(exc).__name__})",
        )

    if engine is not None:
        state = inspect_migrations(settings.database_url, engine)
        record("migrations", state.at_head, ErrorCode.DATABASE_NOT_READY.value, state.summary())
        engine.dispose()

    integrations = [*settings.enabled_adapters]
    if settings.llm_enabled:
        integrations.insert(0, "llm")
    record(
        "optional_integrations",
        not integrations,
        ErrorCode.DEPENDENCY_UNAVAILABLE.value,
        "none enabled" if not integrations else f"configured but unavailable: {integrations}",
    )

    record("api_app", *_check_api_app())
    record("openapi_current", *_check_openapi())

    _finish_doctor(checks, json_output)
    if any(check["status"] == "fail" for check in checks):
        raise typer.Exit(code=1)


def _finish_doctor(
    checks: list[dict[str, Any]], json_output: bool, *, extra: str | None = None
) -> None:
    if json_output:
        typer.echo(json.dumps({"checks": checks}, indent=2))
        return
    for check in checks:
        marker = "ok" if check["status"] == "ok" else "fail"
        typer.echo(f"[{marker}] {check['check']}: {check['message']}")
    if extra:
        typer.echo(extra, err=True)
    failures = sum(1 for check in checks if check["status"] == "fail")
    if failures:
        typer.echo(f"doctor: {failures} check(s) failed")
    else:
        typer.echo("doctor: all checks passed")


def _check_api_app() -> tuple[bool, str | None, str]:
    try:
        from nyx.api.app import create_app
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            ErrorCode.INTERNAL_ERROR.value,
            f"API app not importable ({type(exc).__name__})",
        )
    try:
        create_app()
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            ErrorCode.INTERNAL_ERROR.value,
            f"API app failed to build ({type(exc).__name__})",
        )
    return True, None, "importable and buildable"


def _check_openapi() -> tuple[bool, str | None, str]:
    try:
        from nyx.api.openapi import openapi_path, render_openapi
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            ErrorCode.INTERNAL_ERROR.value,
            f"OpenAPI helper unavailable ({type(exc).__name__})",
        )
    path = openapi_path()
    if not path.exists():
        return False, ErrorCode.NOT_FOUND.value, "docs/openapi.json is missing"
    if path.read_text(encoding="utf-8") != render_openapi():
        return False, ErrorCode.CONFLICT.value, "docs/openapi.json is out of date"
    return True, None, "docs/openapi.json is current"


def _check_data_dir(data_dir: Path) -> bool:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        descriptor, path = tempfile.mkstemp(prefix=".nyx-doctor-", dir=str(data_dir))
        os.close(descriptor)
        Path(path).unlink()
    except OSError:
        return False
    return True


if __name__ == "__main__":
    app()
