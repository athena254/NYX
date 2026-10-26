"""The `nyx golden` command group: the golden set, and the review that makes it real.

Four things a reader can do here, and the split is deliberate:

* **generate** — draft cases from a spec. Requires a provider; without one it refuses and
  drafts nothing. Every case it writes is a *draft*.
* **list** / **pending** — see what is in the set and what is waiting.
* **approve** / **reject** — a human decision, which is the only thing that makes a case
  scorable. The reviewer is required and a rejection needs a reason.
* **export** — write the approved cases in the shape the eval runner consumes.

A generated case is not a test. It is a *proposal*, and until a reviewer approves it NYX will
not score it. The commands are arranged so that is visible: `generate` writes drafts,
`list` shows their status, and `export` emits only approved ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from nyx.config import Settings
from nyx.critics.llm import LlmTransportError
from nyx.critics.llm_transport import SyncCriticTransport
from nyx.error_codes import ErrorCode
from nyx.evals.generate import CaseGenerator
from nyx.evals.review import ReviewError, ReviewQueue
from nyx.evals.spec import parse_spec
from nyx.evals.store import (
    CaseEventType,
    load_events,
    pending_cases,
    scorable_cases,
    write_cases,
)
from nyx.ingestion.errors import StructuredError
from nyx.llm.provider import LLMConfig

golden_app = typer.Typer(
    help="The golden set: draft cases from a spec, review them, export the approved ones.",
    no_args_is_help=True,
)

DEFAULT_CASE_LOG = Path("evals/golden/cases.jsonl")
DEFAULT_EXPORT = Path("evals/golden/golden.jsonl")


def _emit(error: StructuredError, json_output: bool) -> None:
    """Emit a structured error in the CLI's standard shape.

    ``_emit_error`` lives in :mod:`nyx.cli.main`, and this module is imported *by* that one,
    so the import is deferred to call time. The repo already uses this shape for the same
    reason (``nyx.findings.lifecycle`` imports the self-consistency check lazily to avoid a
    cycle), and an import at module scope here would be circular and fail on startup.
    """

    from nyx.cli.main import _emit_error

    _emit_error(error, json_output)


def _log_path(path: Path) -> Path:
    """Resolve the case log, creating nothing.

    The log is created by the first write, not by opening it for read: a read of a missing
    log is an empty set, which is a real state for a deployment that has not generated yet.
    """

    return path


def _read(path: Path) -> list[Any]:
    return load_events(_log_path(path))


@golden_app.command("list")
def list_cases(
    path: Annotated[Path, typer.Option("--log", help="Case log to read.")] = DEFAULT_CASE_LOG,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List every case in the log with its status, in case-id order.

    A draft and an approved case look identical in the inputs, so the status is the field
    that matters here; without it a reader cannot tell a proposal from a test.
    """

    events = _read(path)
    from nyx.evals.store import current_state

    cases = sorted(current_state(events).values(), key=lambda case: case.case_id)
    payload: dict[str, Any] = {
        "total": len(cases),
        "scorable": sum(1 for case in cases if case.is_scorable),
        "pending": sum(1 for case in cases if case.status.value == "draft"),
        "cases": [
            {
                "case_id": case.case_id,
                "status": case.status.value,
                "expected": case.expected.value,
                "spec_ref": case.spec_ref,
                "scorable": case.is_scorable,
                "reviewed_by": case.provenance.reviewed_by,
            }
            for case in cases
        ],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"{payload['total']} case(s); {payload['scorable']} scorable")
    for case in payload["cases"]:
        flag = "scorable" if case["scorable"] else case["status"]
        typer.echo(f"  {case['case_id']:36} {case['expected']:12} {flag}")


@golden_app.command("pending")
def pending(
    path: Annotated[Path, typer.Option("--log", help="Case log to read.")] = DEFAULT_CASE_LOG,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List the cases awaiting review.

    This is the review queue. A long list is not a backlog to clear: each entry is a case
    whose inputs and label somebody has to look at before it counts.
    """

    cases = pending_cases(_read(path))
    payload: dict[str, Any] = {
        "pending": len(cases),
        "cases": [
            {
                "case_id": case.case_id,
                "expected": case.expected.value,
                "label_source": case.provenance.label_source,
                "spec_ref": case.spec_ref,
            }
            for case in cases
        ],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"{payload['pending']} case(s) awaiting review")
    for case in payload["cases"]:
        typer.echo(f"  {case['case_id']:36} proposed {case['expected']}")


def _decide(
    decision: str,
    case_id: str,
    reviewer: str,
    note: str | None,
    path: Path,
    json_output: bool,
) -> None:
    queue = ReviewQueue(_log_path(path), actor=reviewer)
    try:
        if decision == "approve":
            event = queue.approve(case_id, note=note)
        else:
            event = queue.reject(case_id, note=note)
    except ReviewError as exc:
        # ``StructuredError.message`` is capped at 500 characters and carries no free-form
        # detail field, so the stable code goes in the message where a caller can read it.
        _emit(
            StructuredError(
                code=ErrorCode.INVALID_REQUEST.value,
                message=f"{exc.code}: {exc}"[:500],
                request_id="cli",
            ),
            json_output,
        )
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "case_id": event.case_id,
                    "event": event.event.value,
                    "reviewed_by": event.reviewed_by,
                    "at": event.at.isoformat(),
                },
                indent=2,
            )
        )
        return
    typer.echo(f"{event.case_id}: {event.event.value} by {event.reviewed_by}")


@golden_app.command("approve")
def approve(
    case_id: str,
    reviewer: Annotated[
        str, typer.Option("--reviewer", help="Who is approving. Required; never defaulted.")
    ],
    note: Annotated[str | None, typer.Option("--note", help="Optional confirmation note.")] = None,
    path: Annotated[Path, typer.Option("--log", help="Case log to read and append to.")] = (
        DEFAULT_CASE_LOG
    ),
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Approve a case, confirming both its inputs and its proposed label.

    Approval is what makes a case scorable. It records the reviewer as the source of the
    label, so the deterministic rule stops claiming credit for a verdict a person chose.
    """

    _decide("approve", case_id, reviewer, note, path, json_output)


@golden_app.command("reject")
def reject(
    case_id: str,
    reviewer: Annotated[
        str, typer.Option("--reviewer", help="Who is rejecting. Required; never defaulted.")
    ],
    note: Annotated[
        str,
        typer.Option("--note", help="Why. Required: an unexplained rejection is a case dropped."),
    ],
    path: Annotated[Path, typer.Option("--log", help="Case log to read and append to.")] = (
        DEFAULT_CASE_LOG
    ),
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Reject a case, with a reason.

    A rejection is not a failure of the generator — it is a finding about the requirement.
    A clause that cannot be turned into a test is worth knowing about, so the reason is kept.
    """

    _decide("reject", case_id, reviewer, note, path, json_output)


@golden_app.command("generate")
def generate(
    spec: Annotated[
        Path, typer.Option("--spec", help="Spec or plan to read clauses from, e.g. docs/SPEC.md.")
    ],
    path: Annotated[Path, typer.Option("--log", help="Case log to append drafts to.")] = (
        DEFAULT_CASE_LOG
    ),
    model: Annotated[
        str | None, typer.Option("--model", help="Model id. Defaults to NYX_LLM_MODEL.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Draft eval cases from a spec, and append them to the case log as drafts.

    Requires a configured provider. With none, this refuses and writes nothing rather than
    inventing inputs — a generator that fabricates cases is worse than one that says no.

    Drafts are not tests. They go to the review queue, and only an approval makes them
    scorable.
    """

    settings = Settings()
    if not settings.llm_enabled or not settings.llm_model:
        _emit(
            StructuredError(
                code=ErrorCode.INVALID_REQUEST.value,
                message=(
                    "case generation needs a provider. Set NYX_LLM_ENABLED=true and "
                    "NYX_LLM_MODEL, or pass --model with a configured base URL."
                ),
                request_id="cli",
            ),
            json_output,
        )
        raise typer.Exit(1)

    if not spec.exists():
        _emit(
            StructuredError(
                code=ErrorCode.INVALID_REQUEST.value,
                message=f"spec not found: {spec}",
                request_id="cli",
            ),
            json_output,
        )
        raise typer.Exit(1)

    config = LLMConfig(
        protocol=settings.llm_protocol,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
        model=model or settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    transport = SyncCriticTransport(config)
    generator = CaseGenerator(
        transport=transport,
        timeout_seconds=settings.probe_timeout_seconds,
        model_provider=settings.llm_provider,
        model_version=config.model,
        delay_seconds=settings.golden_delay_seconds,
    )

    text = spec.read_text(encoding="utf-8")
    clauses = parse_spec(text, source=str(spec))
    try:
        outcomes = generator.generate(clauses, source=str(spec))
    except LlmTransportError as exc:
        _emit(
            StructuredError(
                code=ErrorCode.DEPENDENCY_UNAVAILABLE.value,
                message=f"the provider could not be reached: {exc}",
                request_id="cli",
            ),
            json_output,
        )
        raise typer.Exit(1) from exc

    drafted = [outcome.case for outcome in outcomes if outcome.case is not None]
    failures = [
        {"clause": outcome.clause.text, "section": outcome.clause.section, "error": outcome.error}
        for outcome in outcomes
        if outcome.error is not None
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    from nyx.domain.base import utc_now
    from nyx.evals.store import CaseEvent, append_event

    with path.open("a", encoding="utf-8") as handle:
        for case in drafted:
            append_event(
                handle,
                CaseEvent(
                    event=CaseEventType.PROPOSED,
                    at=utc_now(),
                    case_id=case.case_id,
                    case=case,
                ),
            )

    payload: dict[str, Any] = {
        "spec": str(spec),
        "clauses": len(clauses),
        "drafted": len(drafted),
        "skipped": sum(1 for outcome in outcomes if outcome.skipped is not None),
        "failed": len(failures),
        "cases": [
            {"case_id": case.case_id, "expected": case.expected.value, "status": case.status.value}
            for case in drafted
        ],
        # A count without a reason is a command that reports a failure it cannot explain. Every
        # clause that did not produce a case is listed with the clause text and the reason, in
        # both output modes, so an operator can see *which* requirement failed and why.
        "failures": failures,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(
        f"{payload['drafted']} drafted from {payload['clauses']} clauses; "
        f"{payload['skipped']} skipped, {payload['failed']} failed"
    )
    for case in payload["cases"]:
        typer.echo(f"  {case['case_id']:36} proposed {case['expected']}")
    for failure in failures:
        typer.echo(f"  FAILED: {failure['clause']}")
        typer.echo(f"          {failure['error']}")
    typer.echo("Drafts are not scorable until approved: nyx golden pending")


@golden_app.command("export")
def export(
    path: Annotated[Path, typer.Option("--log", help="Case log to read.")] = DEFAULT_CASE_LOG,
    out: Annotated[Path, typer.Option("--out", help="Where to write the runner's case list.")] = (
        DEFAULT_EXPORT
    ),
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Write the approved cases in the shape the eval runner consumes.

    Only approved cases are exported. This is the single function that defines the golden
    set, so an unapproved case can never reach the runner by any other route.
    """

    cases = scorable_cases(_read(path))
    write_cases(out, cases)
    payload: dict[str, Any] = {
        "exported": len(cases),
        "path": str(out),
        "case_ids": [c.case_id for c in cases],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"{payload['exported']} approved case(s) written to {payload['path']}")


__all__ = ["golden_app"]
