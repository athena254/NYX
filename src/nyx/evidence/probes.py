"""The probe interface and the five built-in probe kinds.

A probe is how NYX looks at the world instead of reading about it. The interface is deliberately
tiny -- ``run(target, context) -> Observation`` -- because a probe is one narrow act of
observation, and a probe that does two things is two probes with a shared name.

**Read-only by default.** ``Probe.read_only`` is a declared property, not a discovery. A probe that
reports ``False`` mutated something, and :mod:`nyx.evidence.registry` refuses to run it without an
authorisation record behind the mutating-probe gate. A probe cannot grant itself permission by
saying it is read-only: the gate checks the *declaration*, and the observation records it.

The five kinds the brief names are implemented here:

* ``StateCheckProbe`` -- inspect real end state: files, hashes, read-only DB queries, health.
* ``ReexecutionProbe`` -- re-run tests/build/a command at the current subject version.
* ``ToolCallVerificationProbe`` -- confirm a claimed tool call happened, with its arguments.
* ``SideEffectProbe`` -- confirm the effect of an action exists.
* ``LogMetricQueryProbe`` -- read-only queries against logs and metrics.

The first four are real and exercised by tests. ``ToolCallVerificationProbe`` is the exception and
is documented as such: it needs a transcript read path, which is an adapter concern rather than a
probe concern, so it verifies the *shape* it can prove from a stored tool-call record and refuses
what it cannot.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from nyx.domain.base import PrincipalRef, utc_now
from nyx.domain.enums import (
    Environment,
    PrincipalKind,
    ProbeKind,
    ProbeOutcome,
    VerificationStrength,
)
from nyx.domain.observation import Observation

#: What one probe may be pointed at. A target is deliberately untyped: a probe reads whatever its
#: kind needs (a path, a URL, a subject id) and reports what it found. NYX does not interpret the
#: target on the probe's behalf.
Target = str


#: The read-only information a probe needs. ``subject_version`` is the commit/build hash the
#: probe must observe *at*; a probe that observes a different version reports ``NOT_OBSERVED``
#: rather than a result, because a stale observation must not pass as a current one.
@dataclass(frozen=True, slots=True)
class ProbeContext:
    subject: str
    subject_version: str | None = None
    environment: Environment = Environment.LOCAL
    collector: PrincipalRef | None = None
    claim_id: str | None = None
    timeout_seconds: float = 30.0
    extra: Mapping[str, Any] = field(default_factory=dict)

    def with_defaults(self) -> ProbeContext:
        """Return a copy with the collector filled in when the probe did not name one."""

        if self.collector is not None:
            return self
        return ProbeContext(
            subject=self.subject,
            subject_version=self.subject_version,
            environment=self.environment,
            collector=PrincipalRef(id="nyx:probe", kind=PrincipalKind.SYSTEM),
            claim_id=self.claim_id,
            timeout_seconds=self.timeout_seconds,
            extra=self.extra,
        )


@runtime_checkable
class Probe(Protocol):
    """The structural contract every probe implements."""

    probe_id: str
    probe_version: str
    kind: ProbeKind
    read_only: bool
    description: str

    def run(self, target: Target, context: ProbeContext) -> Observation:
        """Observe ``target`` and return what was seen."""
        ...


def default_collector() -> PrincipalRef:
    """The identity stamped on an observation when no collector was named."""

    return PrincipalRef(id="nyx:probe", kind=PrincipalKind.SYSTEM)


def make_observation(
    *,
    probe: Probe,
    target: Target,
    context: ProbeContext,
    outcome: ProbeOutcome,
    detail: str,
    payload: dict[str, Any] | None = None,
    strength: VerificationStrength | None = None,
    artifact: bytes | None = None,
) -> Observation:
    """Build the observation a probe returns.

    Centralised so that every probe produces the same record with the same fields populated, and
    so the artifact hash is computed once, here, rather than per probe.
    """

    return Observation(
        probe_id=probe.probe_id,
        probe_version=probe.probe_version,
        subject=context.subject or target,
        subject_version=context.subject_version,
        collected_at=utc_now(),
        collector=context.collector or default_collector(),
        method=probe.kind,
        result=outcome,
        raw_artifact_hash=None if artifact is None else content_hash(artifact),
        environment=context.environment,
        read_only=probe.read_only,
        verification_strength=(strength if strength is not None else STRENGTH_BY_KIND[probe.kind]),
        relevance_to_claim=context.claim_id,
        detail=detail,
        payload=payload,
    )


def content_hash(raw: bytes) -> str:
    """Return the canonical ``sha256:`` digest used across NYX."""

    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


#: The strength each probe kind is worth, by default. A probe may override this in its
#: observation when what it did is genuinely stronger or weaker than its kind's default.
STRENGTH_BY_KIND: dict[ProbeKind, VerificationStrength] = {
    ProbeKind.STATE_CHECK: VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
    ProbeKind.REEXECUTION: VerificationStrength.RERUN_BY_NYX,
    ProbeKind.TOOL_CALL_VERIFICATION: VerificationStrength.RERUN_BY_NYX,
    ProbeKind.SIDE_EFFECT: VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
    ProbeKind.LOG_METRIC_QUERY: VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
}


class StateCheckProbe:
    """Inspect real end state: a file, a directory listing, or a supplied reader.

    The reader is injected so the probe never decides how to reach a database. A production probe
    supplies a read-only query function; the test supplies a dict. That keeps the probe honest
    about being read-only: it cannot open a connection it was not handed.
    """

    probe_id = "probe:state-check"
    probe_version = "1.0.0"
    kind = ProbeKind.STATE_CHECK
    read_only = True
    description = (
        "Inspect real end state (files, hashes, or a supplied read-only reader) at the current "
        "subject version."
    )

    def run(
        self,
        target: Target,
        context: ProbeContext,
    ) -> Observation:
        reader: Callable[[], bytes | None] | None = context.extra.get("reader")
        path = context.extra.get("path")
        if reader is None and path is None:
            return make_observation(
                probe=self,
                target=target,
                context=context,
                outcome=ProbeOutcome.SKIPPED,
                detail=(
                    "StateCheckProbe was given neither a reader nor a path, so it observed "
                    "nothing. A probe that cannot reach its target reports skipped rather than "
                    "not-observed."
                ),
            )
        try:
            if reader is not None:
                raw = reader()
            else:
                raw = Path(str(path)).read_bytes() if Path(str(path)).is_file() else None
        except OSError as exc:
            return make_observation(
                probe=self,
                target=target,
                context=context,
                outcome=ProbeOutcome.ERROR,
                detail=f"Reading {target} failed: {type(exc).__name__}.",
            )
        if raw is None:
            return make_observation(
                probe=self,
                target=target,
                context=context,
                outcome=ProbeOutcome.NOT_OBSERVED,
                detail=f"No content found at {target}.",
            )
        return make_observation(
            probe=self,
            target=target,
            context=context,
            outcome=ProbeOutcome.OBSERVED,
            detail=f"Read {len(raw)} byte(s) from {target}.",
            payload={"length": len(raw)},
            artifact=raw,
        )


class ReexecutionProbe:
    """Re-run a check at the current subject version rather than reading an old report.

    The command runner is injected. NYX never interprets the command itself: a runner is a
    zero-argument callable returning ``(returncode, stdout, stderr)``, so a test supplies a
    function and a production deployment supplies one that shells out behind its own sandbox.
    """

    probe_id = "probe:reexecution"
    probe_version = "1.0.0"
    kind = ProbeKind.REEXECUTION
    read_only = True
    description = (
        "Re-run a test, build, or command at the current subject version instead of reading a "
        "previously recorded report."
    )

    def run(self, target: Target, context: ProbeContext) -> Observation:
        runner: Callable[[], tuple[int, str, str]] | None = context.extra.get("runner")
        if runner is None:
            return make_observation(
                probe=self,
                target=target,
                context=context,
                outcome=ProbeOutcome.SKIPPED,
                detail="ReexecutionProbe was given no command runner, so it ran nothing.",
            )
        returncode, stdout, stderr = runner()
        payload = {
            "returncode": returncode,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }
        outcome = ProbeOutcome.OBSERVED if returncode == 0 else ProbeOutcome.NOT_OBSERVED
        return make_observation(
            probe=self,
            target=target,
            context=context,
            outcome=outcome,
            detail=(f"Re-executed the check for {target}: exit code {returncode}."),
            payload=payload,
            artifact=f"{returncode}\n{stdout}\n{stderr}".encode("utf-8", errors="replace"),
        )


class ToolCallVerificationProbe:
    """Confirm a claimed tool call occurred, with the arguments that were claimed.

    This probe verifies a *record* of a tool call, not the call itself: it has no transport to
    the agent that made it, and pretending otherwise would let a record be mistaken for an event.
    It compares the stored call against the claim and reports whether they line up, which is the
    most that is provable without a transcript read path.
    """

    probe_id = "probe:tool-call-verification"
    probe_version = "1.0.0"
    kind = ProbeKind.TOOL_CALL_VERIFICATION
    read_only = True
    description = (
        "Confirm a claimed tool call is recorded with matching arguments and an observable result."
    )

    def run(self, target: Target, context: ProbeContext) -> Observation:
        recorded = context.extra.get("recorded_tool_call")
        claimed = context.extra.get("claimed_tool_call")
        if not isinstance(recorded, Mapping) or not isinstance(claimed, Mapping):
            return make_observation(
                probe=self,
                target=target,
                context=context,
                outcome=ProbeOutcome.SKIPPED,
                detail=(
                    "ToolCallVerificationProbe needs both a recorded and a claimed tool call to "
                    "compare; with either missing it verifies nothing."
                ),
            )
        tool = str(recorded.get("tool", ""))
        matches = tool == str(claimed.get("tool", ""))
        arguments = recorded.get("arguments") == claimed.get("arguments")
        had_result = bool(recorded.get("result"))
        if matches and arguments and had_result:
            outcome, detail = (
                ProbeOutcome.OBSERVED,
                f"Recorded tool call '{tool}' matches the claim's tool and arguments and "
                "carries a result.",
            )
        else:
            outcome, detail = (
                ProbeOutcome.NOT_OBSERVED,
                f"Recorded tool call does not fully match the claim (tool match={matches}, "
                f"arguments match={arguments}, result present={had_result}).",
            )
        return make_observation(
            probe=self,
            target=target,
            context=context,
            outcome=outcome,
            detail=detail,
            payload={"recorded": dict(recorded), "claimed": dict(claimed)},
        )


class SideEffectProbe:
    """Confirm the effect of an action exists: the record is saved, the message is queued.

    The existence check is injected as a predicate returning the observed state or ``None``.
    """

    probe_id = "probe:side-effect"
    probe_version = "1.0.0"
    kind = ProbeKind.SIDE_EFFECT
    read_only = True
    description = "Confirm the real downstream effect of an action exists."

    def run(self, target: Target, context: ProbeContext) -> Observation:
        exists: Callable[[], Any] | None = context.extra.get("exists")
        if exists is None:
            return make_observation(
                probe=self,
                target=target,
                context=context,
                outcome=ProbeOutcome.SKIPPED,
                detail="SideEffectProbe was given no existence check, so it observed nothing.",
            )
        observed = exists()
        if observed is None:
            return make_observation(
                probe=self,
                target=target,
                context=context,
                outcome=ProbeOutcome.NOT_OBSERVED,
                detail=f"The claimed effect of '{target}' was not found.",
            )
        return make_observation(
            probe=self,
            target=target,
            context=context,
            outcome=ProbeOutcome.OBSERVED,
            detail=f"The effect of '{target}' exists; observed state: {observed!r}.",
            payload={"observed": repr(observed)},
        )


class LogMetricQueryProbe:
    """Read-only queries against logs and metrics for a window.

    The query function is injected. ``read_only`` is ``True`` and must stay so: a probe that
    writes to a log or a metric is not a log query.
    """

    probe_id = "probe:log-metric-query"
    probe_version = "1.0.0"
    kind = ProbeKind.LOG_METRIC_QUERY
    read_only = True
    description = "Run a read-only query against logs and metrics for the relevant window."

    def run(self, target: Target, context: ProbeContext) -> Observation:
        query: Callable[[datetime], list[Mapping[str, Any]]] | None = context.extra.get("query")
        if query is None:
            return make_observation(
                probe=self,
                target=target,
                context=context,
                outcome=ProbeOutcome.SKIPPED,
                detail="LogMetricQueryProbe was given no query function, so it queried nothing.",
            )
        window_start = context.extra.get("window_start")
        rows = query(window_start if isinstance(window_start, datetime) else utc_now())
        return make_observation(
            probe=self,
            target=target,
            context=context,
            outcome=ProbeOutcome.OBSERVED if rows else ProbeOutcome.NOT_OBSERVED,
            detail=f"Log/metric query for {target} returned {len(rows)} row(s).",
            payload={"rows": [dict(row) for row in rows]},
        )


def builtin_probes() -> tuple[Probe, ...]:
    """Return the five built-in probes in registration order."""

    return (
        StateCheckProbe(),
        ReexecutionProbe(),
        ToolCallVerificationProbe(),
        SideEffectProbe(),
        LogMetricQueryProbe(),
    )


__all__ = [
    "LogMetricQueryProbe",
    "Probe",
    "ProbeContext",
    "ReexecutionProbe",
    "SideEffectProbe",
    "StateCheckProbe",
    "ToolCallVerificationProbe",
    "builtin_probes",
    "content_hash",
    "default_collector",
    "make_observation",
]
