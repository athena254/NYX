"""NYX command-line interface (SPEC §10, Phase 1 subset).

Every command supports ``--json`` for machine-readable output, exits non-zero on failure, and
never prints secrets: it prints structured error codes and safe messages only. The CLI calls
the same :class:`IngestionService` as the HTTP API; it does not write to the database directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from nyx.audit.verifier import VerificationReport, verify_chain
from nyx.auth.checker import ConfigBackedCapabilityChecker
from nyx.auth.store import ANONYMOUS_PRINCIPAL_ID, CapabilityStore
from nyx.config import Settings
from nyx.conversational.agent import (
    ChatAgent,
    ChatError,
    assert_no_judge_tools,
    build_llm_config,
)
from nyx.conversational.client import NyxClient, NyxClientError
from nyx.conversational.llm import build_client
from nyx.conversational.mcp_server import tool_names as mcp_tool_names
from nyx.conversational.telegram.adapter import TelegramError
from nyx.conversational.telegram.bot import TelegramBot
from nyx.conversational.telegram.config import TelegramConfig
from nyx.critics import register_storage_backed
from nyx.critics.registry import CriticRegistryError, list_critics
from nyx.disputes.service import (
    DisputeError,
    DisputeService,
    FindingNotFoundError,
    UnknownEvidenceError,
)
from nyx.disputes.verification import (
    VerificationNotPermittedError,
    default_allowlist,
)
from nyx.domain.base import PrincipalRef, utc_now
from nyx.domain.enums import (
    DispositionOutcome,
    FindingStatus,
    PrincipalKind,
    RebuttalRequestedOutcome,
    Severity,
)
from nyx.domain.ids import new_request_id
from nyx.error_codes import ErrorCode
from nyx.findings.lifecycle import (
    Capability,
    CapabilityDeniedError,
    InvalidLifecycleTransitionError,
)
from nyx.findings.service import FindingQuery, FindingService
from nyx.ingestion.errors import IngestResult, StructuredError
from nyx.ingestion.limits import LimitExceededError
from nyx.ingestion.service import IngestionService
from nyx.retention.policy import RetentionPolicy
from nyx.retention.redaction import RedactionDeniedError, RedactionService, RetentionAuthoriser
from nyx.retention.service import RetentionService
from nyx.storage.db import (
    create_engine_from_settings,
    create_session_factory,
    session_scope,
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
critic_app = typer.Typer(help="List and run the deterministic critics.", no_args_is_help=True)
finding_app = typer.Typer(help="List and inspect findings.", no_args_is_help=True)
chat_app = typer.Typer(help="Talk to NYX.", no_args_is_help=True)
telegram_app = typer.Typer(help="Run the Telegram front-end.", no_args_is_help=True)
audit_app = typer.Typer(help="Verify the append-only audit history.", no_args_is_help=True)
retention_app = typer.Typer(
    help="Retention policy and the redaction workflow.", no_args_is_help=True
)

app.add_typer(system_app, name="system")
app.add_typer(ingest_app, name="ingest")
app.add_typer(claim_app, name="claim")
app.add_typer(evidence_app, name="evidence")
app.add_typer(critic_app, name="critic")
app.add_typer(finding_app, name="finding")
app.add_typer(chat_app, name="chat")
app.add_typer(telegram_app, name="telegram")
app.add_typer(audit_app, name="audit")
app.add_typer(retention_app, name="retention")


@app.callback()
def main() -> None:
    """NYX evidence-backed oversight service."""


# --------------------------------------------------------------------------- helpers


def _read_raw(path: str | None, json_output: bool) -> bytes:
    """Read a command's input file, or stdin for ``-``.

    A missing file is reported as a structured error with a non-zero exit, not a traceback: the
    CLI is a machine interface, and a caller that cannot open the file needs the same code it
    would get for a malformed one.
    """

    if path is None or path == "-":
        return sys.stdin.buffer.read()
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        _emit_error(
            StructuredError(
                code=ErrorCode.INVALID_REQUEST.value,
                message=f"could not read input file {path!r} ({type(exc).__name__})",
                request_id="cli",
            ),
            json_output,
        )
        raise typer.Exit(code=1) from exc


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


def _finding_service() -> FindingService:
    settings = Settings()
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    # Keep the CLI's critic set identical to the API's: C-010 audits NYX's own store, so it
    # must be handed the same session factory the CLI just built.
    register_storage_backed(factory)
    return FindingService(factory)


@contextmanager
def _dispute_session() -> Iterator[DisputeService]:
    """Yield a dispute service over one transaction.

    The CLI has no HTTP request to hang a transaction on, so each command opens exactly one. It
    acts as the local operator (see :func:`_cli_actor`) against the configuration-backed checker,
    which is the same identity an unauthenticated loopback request receives (ADR 0007).
    """

    settings = Settings()
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            yield DisputeService(
                session,
                capability_checker=ConfigBackedCapabilityChecker(
                    CapabilityStore.load_from_settings(settings)
                ),
                allowlist=default_allowlist(),
            )
    finally:
        engine.dispose()


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

    raw = _read_raw(manifest, json_output)
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

    raw = _read_raw(source, json_output)
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

    raw = _read_raw(source, json_output)
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

    raw = _read_raw(source, json_output)
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

    raw = _read_raw(source, json_output)
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
    # An enabled optional integration is a *configuration*, not a fault. This check used to record
    # a failure for any configured integration while never verifying availability, so a correct
    # deployment failed `nyx doctor`. It now reports what is configured and leaves the
    # reachability probe to a future change rather than claiming an availability it never checked.
    record(
        "optional_integrations",
        True,
        None,
        "none enabled" if not integrations else f"configured: {', '.join(integrations)}",
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


# --------------------------------------------------------------------------- audit


def _audit_report() -> VerificationReport:
    """Verify the chain over one short-lived engine, then release it."""

    engine = create_engine_from_settings(Settings())
    try:
        return verify_chain(create_session_factory(engine))
    finally:
        engine.dispose()


@audit_app.command("verify")
def audit_verify(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Verify the audit hash chain and report its first divergence.

    Exits 0 on a clean chain and 1 on divergence. The chain detects accidental mutation within the
    stored sequence; it is not a defence against an attacker who rewrites the whole database and
    recomputes every hash (SPEC §5.8).
    """

    try:
        report = _audit_report()
    except Exception as exc:  # noqa: BLE001
        _emit_error(
            StructuredError(
                code=ErrorCode.INTERNAL_ERROR.value,
                message=f"could not verify the audit chain ({type(exc).__name__})",
                request_id="cli",
            ),
            json_output,
        )
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json.dumps(report.as_dict(), indent=2))
    elif report.ok:
        typer.echo(f"audit chain ok: {report.entries_checked} entries verified")
    else:
        assert report.first_divergence is not None  # nosec B101 - ok=False implies divergence
        divergence = report.first_divergence
        typer.echo(
            f"audit chain diverged at sequence {divergence.sequence}: {divergence.reason}",
            err=True,
        )
        typer.echo(f"entry: {divergence.entry_id}", err=True)

    if not report.ok:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- critic


@critic_app.command("list")
def critic_list(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List the registered deterministic critics and their declared scope."""

    critics = list_critics()
    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "critic_id": item.critic_id,
                        "critic_version": item.critic_version,
                        "description": item.description,
                        "can_assess": item.can_assess,
                        "cannot_assess": item.cannot_assess,
                    }
                    for item in critics
                ],
                indent=2,
            )
        )
        return
    for item in critics:
        typer.echo(f"{item.critic_id}  v{item.critic_version}  {item.description}")


@critic_app.command("run")
def critic_run(
    event: str = typer.Option(..., "--event", help="Stable id of the event to assess."),
    critic: list[str] | None = typer.Option(
        None, "--critic", help="Critic id to run; repeat for several. Omit for the whole pack."
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Run the selected critics against one event.

    A ``no_finding`` result means only that a critic found nothing within its declared scope.
    It is never a statement that the claim is true.
    """

    try:
        outcomes = _finding_service().run_critics(event, list(critic) if critic else None)
    except CriticRegistryError as exc:
        _emit_error(
            StructuredError(
                code=ErrorCode.INVALID_REQUEST.value,
                message=str(exc),
                request_id="cli",
            ),
            json_output,
        )
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "critic_id": item.critic_id,
                        "critic_version": item.critic_version,
                        "result": item.result.value,
                        "proposition": item.proposition,
                        "rationale": item.rationale,
                        "finding_id": item.finding_id,
                    }
                    for item in outcomes
                ],
                indent=2,
            )
        )
        return
    for item in outcomes:
        line = f"{item.critic_id}: {item.result.value}"
        if item.finding_id:
            line += f"  finding={item.finding_id}"
        typer.echo(line)


# --------------------------------------------------------------------------- finding


@finding_app.command("list")
def finding_list(
    status: FindingStatus | None = typer.Option(None, "--status", help="Filter by status."),
    severity: Severity | None = typer.Option(None, "--severity", help="Filter by severity."),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List findings in creation order."""

    page = _finding_service().list_findings(
        FindingQuery(status=status, severity=severity, limit=limit)
    )
    if json_output:
        typer.echo(json.dumps(page.model_dump(mode="json", by_alias=True), indent=2))
        return
    if not page.items:
        typer.echo("no findings")
        return
    for finding in page.items:
        typer.echo(
            f"{finding.id}  {finding.severity.value:<8} {finding.status.value:<12} {finding.title}"
        )


@finding_app.command("show")
def finding_show(
    finding_id: str = typer.Argument(..., help="Stable id of the finding."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show one finding, including its evidence links, limitations and unknowns."""

    finding = _finding_service().get_finding(finding_id)
    if finding is None:
        _emit_error(
            StructuredError(
                code=ErrorCode.NOT_FOUND.value,
                message="Finding not found.",
                request_id="cli",
            ),
            json_output,
        )
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(finding.model_dump(mode="json", by_alias=True), indent=2))
        return
    typer.echo(f"{finding.id}  {finding.severity.value}  {finding.status.value}")
    typer.echo(finding.title)
    typer.echo("")
    typer.echo(finding.summary)
    if finding.limitations:
        typer.echo("")
        typer.echo("limitations:")
        for item in finding.limitations:
            typer.echo(f"  - {item}")


# --------------------------------------------------------------------------- dispute


def _dispute_error(code: ErrorCode, message: str, json_output: bool) -> None:
    _emit_error(StructuredError(code=code.value, message=message, request_id="cli"), json_output)


def _cli_actor() -> PrincipalRef:
    """Return the identity the CLI acts as: the local operator.

    The CLI opens the database directly and is outside HTTP authentication (ADR 0007), so it acts
    as the loopback local operator -- the same identity an unauthenticated loopback request
    receives. The declared identity is still recorded on the row; the actor is what must hold the
    capability. Local shell access already implies database write access, so this grants nothing an
    operator could not do directly with ``sqlite3``.
    """

    return PrincipalRef(
        id=ANONYMOUS_PRINCIPAL_ID,
        kind=PrincipalKind.HUMAN,
        display_name="Local operator (CLI)",
    )


@finding_app.command("rebut")
def finding_rebut(
    finding_id: str = typer.Argument(..., help="Stable id of the finding being challenged."),
    source: str = typer.Option("-", "--file", help="Rebuttal JSON file, or '-' for stdin."),
    submitted_by: str = typer.Option(..., "--submitted-by", help="Identity of the challenger."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Challenge a finding. The rebuttal appends to history; the finding is not overwritten."""

    document = _load_json(_read_raw(source, json_output), json_output)
    if not isinstance(document, dict):
        _dispute_error(
            ErrorCode.INVALID_REQUEST, "Rebuttal input must be a JSON object.", json_output
        )
        raise typer.Exit(code=1)

    try:
        with _dispute_session() as dispute:
            rebuttal = dispute.submit_rebuttal(
                finding_id=finding_id,
                actor=_cli_actor(),
                submitted_by=submitted_by,
                argument=str(document.get("argument", "")),
                evidence_ids=list(document.get("evidence_ids", [])),
                requested_outcome=RebuttalRequestedOutcome(
                    str(document.get("requested_outcome", "no_change"))
                ),
            )
    except UnknownEvidenceError as exc:
        _dispute_error(ErrorCode.UNKNOWN_EVIDENCE_ID, exc.detail, json_output)
        raise typer.Exit(code=1) from exc
    except FindingNotFoundError as exc:
        _dispute_error(ErrorCode.NOT_FOUND, exc.detail, json_output)
        raise typer.Exit(code=1) from exc
    except (ValidationError, DisputeError) as exc:
        _dispute_error(ErrorCode.INVALID_REQUEST, str(exc), json_output)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json.dumps(rebuttal.model_dump(mode="json", by_alias=True), indent=2))
        return
    typer.echo(f"rebuttal {rebuttal.id} recorded against {finding_id}")


@finding_app.command("verify")
def finding_verify(
    finding_id: str = typer.Argument(..., help="Stable id of the finding to verify."),
    check: str = typer.Option("critics.rerun-linked", "--check", help="Allowlisted check id."),
    requested_by: str = typer.Option("principal_cli", "--requested-by"),
    run: bool = typer.Option(False, "--run", help="Run queued jobs immediately."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Request an allowlisted verification check, then optionally run it."""

    try:
        with _dispute_session() as dispute:
            job = dispute.request_verification(
                finding_id=finding_id, check_id=check, requested_by=requested_by
            )
            jobs = dispute.run_pending_verification(finding_id=finding_id) if run else [job]
    except VerificationNotPermittedError as exc:
        _dispute_error(ErrorCode.VERIFICATION_NOT_PERMITTED, exc.detail, json_output)
        raise typer.Exit(code=1) from exc
    except FindingNotFoundError as exc:
        _dispute_error(ErrorCode.NOT_FOUND, exc.detail, json_output)
        raise typer.Exit(code=1) from exc
    except DisputeError as exc:
        _dispute_error(ErrorCode.INVALID_REQUEST, exc.detail, json_output)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json.dumps([item.model_dump(mode="json", by_alias=True) for item in jobs], indent=2)
        )
        return
    for item in jobs:
        typer.echo(f"{item.id}  {item.check_id}  {item.status.value}")


@finding_app.command("dispose")
def finding_dispose(
    finding_id: str = typer.Argument(..., help="Stable id of the finding."),
    outcome: DispositionOutcome = typer.Option(..., "--outcome"),
    reason: str = typer.Option(..., "--reason", help="Why this outcome was reached."),
    decided_by: str = typer.Option("principal_cli", "--decided-by"),
    policy_version: str = typer.Option("1.0", "--policy-version"),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Record an authorised outcome. Requires the disposition capability (Phase 4)."""

    try:
        with _dispute_session() as dispute:
            disposition = dispute.record_disposition(
                finding_id=finding_id,
                actor=_cli_actor(),
                outcome=outcome,
                rationale=reason,
                decided_by=decided_by,
                policy_version=policy_version,
            )
    except CapabilityDeniedError as exc:
        _dispute_error(ErrorCode.CAPABILITY_REQUIRED, exc.detail, json_output)
        raise typer.Exit(code=1) from exc
    except InvalidLifecycleTransitionError as exc:
        _dispute_error(ErrorCode.INVALID_LIFECYCLE_TRANSITION, exc.detail, json_output)
        raise typer.Exit(code=1) from exc
    except FindingNotFoundError as exc:
        _dispute_error(ErrorCode.NOT_FOUND, exc.detail, json_output)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json.dumps(disposition.model_dump(mode="json", by_alias=True), indent=2))
        return
    typer.echo(f"disposition {disposition.id} recorded: {outcome.value}")


@finding_app.command("history")
def finding_history(
    finding_id: str = typer.Argument(..., help="Stable id of the finding."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show the full dispute record: finding, rebuttals, verification, disposition chain."""

    try:
        with _dispute_session() as dispute:
            history = dispute.get_history(finding_id=finding_id)
    except FindingNotFoundError as exc:
        _dispute_error(ErrorCode.NOT_FOUND, exc.detail, json_output)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "finding": history.finding.model_dump(mode="json", by_alias=True),
                    "rebuttals": [
                        item.model_dump(mode="json", by_alias=True) for item in history.rebuttals
                    ],
                    "verification_jobs": [
                        item.model_dump(mode="json", by_alias=True)
                        for item in history.verification_jobs
                    ],
                    "dispositions": [
                        item.model_dump(mode="json", by_alias=True) for item in history.dispositions
                    ],
                },
                indent=2,
            )
        )
        return

    typer.echo(f"{history.finding.id}  {history.finding.status.value}")
    typer.echo("")
    typer.echo(f"rebuttals: {len(history.rebuttals)}")
    for rebuttal in history.rebuttals:
        typer.echo(f"  {rebuttal.id}  {rebuttal.submitted_by}  {rebuttal.requested_outcome.value}")
    typer.echo(f"verification jobs: {len(history.verification_jobs)}")
    for job in history.verification_jobs:
        typer.echo(f"  {job.id}  {job.check_id}  {job.status.value}")
    typer.echo(f"dispositions: {len(history.dispositions)}")
    for disposition in history.dispositions:
        supersedes = disposition.supersedes_disposition_id or "-"
        typer.echo(f"  {disposition.id}  {disposition.outcome.value}  supersedes={supersedes}")


# --------------------------------------------------------------------------- chat


def _chat_refuse(message: str, json_output: bool) -> None:
    """Report a chat failure without a stack trace, then exit non-zero."""

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "code": ErrorCode.INVALID_REQUEST.value,
                    "message": message,
                    "request_id": "cli",
                },
                indent=2,
            )
        )
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=1)


@chat_app.callback()
def chat_main(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Talk to NYX. The model answers from NYX's record; it never judges."""


@chat_app.command("ask")
def chat_ask(
    question: str = typer.Argument(..., help="What to ask NYX."),
    actor: str = typer.Option("cli-user", "--actor", help="Who is asking, for the audit trail."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Ask NYX one question and print the answer."""

    _require_llm()
    agent = _build_agent(actor=actor)
    try:
        answer = agent.run(question)
    except ChatError as exc:
        _chat_refuse(str(exc), json_output)

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "answer": answer,
                    "touched": agent.touched,
                    "tool_calls": len([m for m in agent.history if m.tool_calls]),
                },
                indent=2,
            )
        )
        return
    typer.echo(answer)
    if agent.touched:
        typer.echo(f"\nNYX records touched: {', '.join(agent.touched)}", err=True)


@chat_app.command("shell")
def chat_shell(
    actor: str = typer.Option("cli-user", "--actor", help="Who is asking, for the audit trail."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Interactive chat with NYX. Ctrl-D or an empty line exits."""

    _require_llm()
    agent = _build_agent(actor=actor)
    typer.echo("NYX chat. The model answers from NYX's record; it cannot judge.")
    typer.echo("Type a question. Empty line or Ctrl-D exits.\n", err=True)
    while True:
        try:
            question = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo()
            break
        if not question:
            break
        try:
            answer = agent.run(question)
        except ChatError as exc:
            typer.echo(str(exc), err=True)
            continue
        if json_output:
            typer.echo(json.dumps({"answer": answer}, indent=2))
        else:
            typer.echo(f"nyx> {answer}")
    if agent.touched:
        typer.echo(f"NYX records touched this session: {', '.join(agent.touched)}", err=True)


def _require_llm() -> None:
    """Refuse to start if the LLM is not configured, with an actionable message.

    ``Settings`` itself raises when ``NYX_LLM_ENABLED=true`` is set without a model, so the raw
    ``ValidationError`` is caught and re-reported as a structured refusal. Without this the CLI
    printed a pydantic traceback and no error code -- a caller could not branch on it, which is
    exactly the failure mode the chat shell is supposed to avoid.
    """

    try:
        settings = Settings()
    except ValidationError as exc:
        # A model_validator failure carries no field loc, so match on the message the validator
        # itself raises rather than on a field name.
        message = str(exc)
        if "NYX_LLM_MODEL" in message or "NYX_LLM_ENABLED" in message:
            _chat_refuse(
                "NYX_LLM_MODEL is not set; write it into .env -- "
                "the chat shell will not guess a model.",
                True,
            )
        _chat_refuse(f"invalid NYX configuration: {message.splitlines()[0]}", True)
    if not settings.llm_enabled:
        _chat_refuse(
            "NYX_LLM_ENABLED is false. Set it to true in .env, with NYX_LLM_MODEL, "
            "NYX_LLM_BASE_URL and (if the provider needs one) NYX_LLM_API_KEY.",
            True,
        )
    if not settings.llm_model:
        _chat_refuse("NYX_LLM_MODEL is not set; the chat shell will not guess a model.", True)


def _build_agent(*, actor: str) -> ChatAgent:
    """Build the chat agent from settings, checking NYX is reachable first."""

    try:
        settings = Settings()
    except ValidationError as exc:
        _chat_refuse(
            "invalid NYX configuration: " + (str(exc).splitlines()[0] if str(exc) else ""),
            True,
        )
    assert settings.llm_model is not None  # nosec B101 - _require_llm() guarantees this
    config = build_llm_config(
        protocol=settings.llm_protocol,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    client = NyxClient(base_url=f"http://{settings.host}:{settings.port}")
    try:
        client.list_systems(limit=1)
    except NyxClientError as exc:
        _chat_refuse(
            f"NYX is not reachable at {client.base_url} ({exc.code}). "
            "Start it with: uv run python -m uvicorn nyx.api.app:create_app --factory",
            True,
        )
    assert_no_judge_tools(mcp_tool_names())
    return ChatAgent(
        build_client(config),
        client,
        actor=actor,
        max_rounds=settings.llm_max_tool_rounds,
    )


# --------------------------------------------------------------------------- telegram


def _require_telegram() -> Settings:
    """Refuse to start the bot unless it is configured, naming the missing variable.

    ``Settings`` raises on ``NYX_TELEGRAM_ENABLED=true`` without a token or an allowlist,
    so the raw ``ValidationError`` is caught and re-reported as a structured refusal. A bot
    that starts without an allowlist is a bot that answers anyone who finds it, which is
    the one thing it must never do.
    """

    try:
        settings = Settings()
    except ValidationError as exc:
        message = str(exc)
        if "NYX_TELEGRAM_BOT_TOKEN" in message:
            _chat_refuse(
                "NYX_TELEGRAM_BOT_TOKEN is not set; write it into .env. "
                "The bot will not run without a token.",
                True,
            )
        if "NYX_TELEGRAM_ALLOWED_USER_IDS" in message:
            _chat_refuse(
                "NYX_TELEGRAM_ALLOWED_USER_IDS is not set; list at least one Telegram user id "
                "in .env. The bot refuses to serve strangers.",
                True,
            )
        _chat_refuse(f"invalid NYX configuration: {message.splitlines()[0]}", True)
    if not settings.telegram_enabled:
        _chat_refuse(
            "NYX_TELEGRAM_ENABLED is false. Set it to true in .env, with "
            "NYX_TELEGRAM_BOT_TOKEN and NYX_TELEGRAM_ALLOWED_USER_IDS.",
            True,
        )
    return settings


def _telegram_config(settings: Settings) -> TelegramConfig:
    """Turn NYX settings into the bot's own config, extracting the token exactly once."""

    assert settings.telegram_bot_token is not None  # nosec B101 - _require_telegram() guarantees this
    return TelegramConfig(
        bot_token=settings.telegram_bot_token.get_secret_value(),
        api_base=settings.telegram_api_base,
        allowed_user_ids=frozenset(settings.telegram_allowed_user_ids),
        allowed_chat_ids=frozenset(settings.telegram_allowed_chat_ids),
        require_private_chat=settings.telegram_require_private_chat,
        max_message_length=settings.telegram_max_message_length,
        timeout_seconds=settings.telegram_timeout_seconds,
        poll_timeout_seconds=settings.telegram_poll_timeout_seconds,
        rate_limit_requests=settings.telegram_rate_limit_requests,
        rate_limit_window_seconds=settings.telegram_rate_limit_window_seconds,
        actor_prefix=settings.telegram_actor_prefix,
    )


def _build_telegram_bot() -> TelegramBot:
    """Build the bot from settings, checking both NYX and the model are usable first."""

    _require_telegram()
    _require_llm()
    settings = Settings()
    assert settings.telegram_bot_token is not None  # nosec B101
    assert settings.llm_model is not None  # nosec B101
    llm_config = build_llm_config(
        protocol=settings.llm_protocol,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    client = NyxClient(base_url=f"http://{settings.host}:{settings.port}")
    try:
        client.list_systems(limit=1)
    except NyxClientError as exc:
        _chat_refuse(
            f"NYX is not reachable at {client.base_url} ({exc.code}). "
            "Start it with: uv run python -m uvicorn nyx.api.app:create_app --factory",
            True,
        )
    # The bot inherits the chat agent's safety property, but it is asserted here as well: a
    # bot is a new entry point, and a judge-capable tool must fail the build, not ship.
    assert_no_judge_tools(mcp_tool_names())
    llm = build_client(llm_config)

    def agent_factory(actor: str) -> ChatAgent:
        return ChatAgent(
            llm,
            client,
            actor=actor,
            max_rounds=settings.llm_max_tool_rounds,
        )

    return TelegramBot(_telegram_config(settings), agent_factory=agent_factory)


@telegram_app.command("check")
def telegram_check(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Verify the bot's configuration and that Telegram accepts its token."""

    bot = _build_telegram_bot()

    async def _probe() -> dict[str, Any]:
        try:
            return await bot.adapter.get_me()
        finally:
            await bot.adapter.close()

    try:
        me = asyncio.run(_probe())
    except TelegramError as exc:
        _chat_refuse(f"Telegram rejected the bot token ({exc.kind}).", json_output)
        return

    payload = {
        "bot": {
            "id": me.get("id"),
            "username": me.get("username"),
            "first_name": me.get("first_name"),
        },
        "config": bot.describe(),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"bot: @{payload['bot']['username']} (id {payload['bot']['id']})")
    typer.echo(json.dumps(payload["config"], indent=2, sort_keys=True))


@telegram_app.command("serve")
def telegram_serve(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Poll Telegram and answer messages until stopped (Ctrl-C)."""

    bot = _build_telegram_bot()
    if json_output:
        typer.echo(json.dumps({"status": "starting", "config": bot.describe()}, indent=2))
    else:
        typer.echo("NYX Telegram bot. Ctrl-C to stop.", err=True)

    async def _serve() -> None:
        try:
            await bot.run()
        finally:
            await bot.close()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        typer.echo("stopped.", err=True)


# --------------------------------------------------------------------------- retention


class _StoreBackedRetentionAuthoriser:
    """Answer the retention capability question from the configured principal store.

    :class:`~nyx.retention.redaction.RedactionService` asks a narrow question (``may_redact``)
    because the lifecycle capability enum predates retention. This adapter is where that question is
    answered with a real capability, so the retention workflow and the rest of NYX share one
    authorisation model instead of growing a second one.
    """

    def __init__(self, store: CapabilityStore) -> None:
        self._store = store

    def may_redact(self, actor: PrincipalRef, target_type: str, action: str) -> bool:
        return Capability.RETENTION in self._store.capabilities_for(actor.id)


def _retention_authoriser(settings: Settings) -> RetentionAuthoriser:
    return _StoreBackedRetentionAuthoriser(CapabilityStore.load_from_settings(settings))


@retention_app.command("purge")
def retention_purge(
    apply: bool = typer.Option(
        False, "--apply", help="Actually purge. Without this, only report what would be purged."
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Tombstone payloads whose retention period has expired.

    Destructive, so it is opt-in twice over: ``NYX_RETENTION_PURGE_ENABLED`` must be true for
    ``--apply``, and without ``--apply`` nothing is written -- not even an audit entry, because an
    audit entry is a write. ``legal_hold`` records are never purged.
    """

    settings = Settings()
    if apply and not settings.retention_purge_enabled:
        _emit_error(
            StructuredError(
                code=ErrorCode.FORBIDDEN.value,
                message=(
                    "retention purge is disabled; set NYX_RETENTION_PURGE_ENABLED=true to enable "
                    "it. A dry run needs no flag."
                ),
                request_id="cli",
            ),
            json_output,
        )
        raise typer.Exit(code=1)

    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            report = RetentionService(
                session,
                policy=RetentionPolicy.from_settings(settings),
                actor=_cli_actor(),
            ).purge_due(now=utc_now(), dry_run=not apply)
    finally:
        engine.dispose()

    payload = {
        "scanned": report.scanned,
        "purged": report.purged,
        "skipped_legal_hold": report.skipped_legal_hold,
        "dry_run": report.dry_run,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    if report.dry_run:
        typer.echo(
            f"dry run: {report.purged} of {report.scanned} records would be purged "
            f"({report.skipped_legal_hold} on legal hold)"
        )
        return
    typer.echo(
        f"purged {report.purged} of {report.scanned} records "
        f"({report.skipped_legal_hold} on legal hold)"
    )


@retention_app.command("redact")
def retention_redact(
    record_id: str = typer.Option(..., "--record-id", help="Stable id of the record."),
    target_type: str = typer.Option(
        "evidence",
        "--target-type",
        help="event | claim | evidence. Append-only records are refused.",
    ),
    action: str = typer.Option(
        "payload_redacted", "--action", help="payload_redacted | hash_removed."
    ),
    reason: str = typer.Option(..., "--reason", help="Why the content is being removed."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Tombstone one record's payload, preserving the fact of the change.

    The row is not deleted: its identity and relationships survive, which is what keeps a
    disposition that relied on the evidence inspectable. Findings, rebuttals, dispositions,
    verification jobs, and policy versions are append-only and cannot be named here.
    """

    settings = Settings()
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            outcome = RedactionService(
                session,
                actor=_cli_actor(),
                allowlist=_retention_authoriser(settings),
            ).redact(
                target_type=target_type,
                target_id=record_id,
                action=action,
                reason=reason,
            )
    except RedactionDeniedError as exc:
        _emit_error(
            StructuredError(code=exc.code.value, message=exc.reason, request_id="cli"),
            json_output,
        )
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    payload = {
        "record_id": outcome.record_id,
        "action": outcome.action,
        "fields_affected": list(outcome.fields_affected),
        "audit_entry_id": outcome.audit_entry_id,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(
        f"redacted {outcome.record_id}: {outcome.action} on "
        f"{', '.join(outcome.fields_affected)} (audit {outcome.audit_entry_id})"
    )


if __name__ == "__main__":
    app()
