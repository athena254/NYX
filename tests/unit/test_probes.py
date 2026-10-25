"""Tests for the probe interface and the five built-in probe kinds.

Each probe is exercised in the three states that matter: it finds what it looked for, it does not
find it, and it could not look at all. The third is the one that is easy to omit, and it is the
one that distinguishes an honest probe from a broken one: a probe that reports ``not_observed``
when it never ran would let a collector look clean.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from nyx.domain.base import PrincipalRef, utc_now
from nyx.domain.enums import (
    Environment,
    PrincipalKind,
    ProbeKind,
    ProbeOutcome,
    VerificationStrength,
)
from nyx.evidence.probes import (
    LogMetricQueryProbe,
    ProbeContext,
    ReexecutionProbe,
    SideEffectProbe,
    StateCheckProbe,
    ToolCallVerificationProbe,
    builtin_probes,
    content_hash,
    default_collector,
    make_observation,
)
from nyx.evidence.probes import Probe as ProbeProtocol

#: ProbeContext has no keyword for a probe's own arguments -- they live in ``extra`` -- so the
#: helper routes everything that is not a declared field into it.
_CONTEXT_FIELDS = frozenset(
    {
        "subject",
        "subject_version",
        "environment",
        "collector",
        "claim_id",
        "timeout_seconds",
        "extra",
    }
)

_COLLECTOR = PrincipalRef(id="nyx:probe", kind=PrincipalKind.SYSTEM)


def _context(**overrides: object) -> ProbeContext:
    fields: dict[str, object] = {
        "subject": "booking-service",
        "subject_version": "abc123",
        "environment": Environment.STAGING,
        "collector": _COLLECTOR,
        "claim_id": "clm_1",
    }
    extra: dict[str, object] = {}
    for key, value in overrides.items():
        if key in _CONTEXT_FIELDS:
            fields[key] = value
        else:
            extra[key] = value
    return ProbeContext(extra=extra, **fields)  # type: ignore[arg-type]


# --- the protocol ----------------------------------------------------------


def test_every_builtin_probe_satisfies_the_probe_protocol() -> None:
    """The registry registers against ``Probe``; each built-in must actually be one."""

    for probe in builtin_probes():
        assert isinstance(probe, ProbeProtocol), probe.probe_id
        assert probe.probe_id.startswith("probe:")
        assert probe.probe_version


def test_five_probe_kinds_are_registered() -> None:
    kinds = {probe.kind for probe in builtin_probes()}
    assert kinds == {
        ProbeKind.STATE_CHECK,
        ProbeKind.REEXECUTION,
        ProbeKind.TOOL_CALL_VERIFICATION,
        ProbeKind.SIDE_EFFECT,
        ProbeKind.LOG_METRIC_QUERY,
    }


def test_every_builtin_probe_is_read_only() -> None:
    """Read-only by default is the brief's rule 5, and it holds for all five kinds."""

    for probe in builtin_probes():
        assert probe.read_only is True, probe.probe_id


# --- state check -----------------------------------------------------------


def test_state_check_observes_a_file() -> None:
    observation = StateCheckProbe().run(
        "booking.db",
        _context(path="examples/claim.json"),
    )
    assert observation.result is ProbeOutcome.OBSERVED
    assert observation.raw_artifact_hash is not None
    assert observation.verification_strength is (
        VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE
    )
    assert observation.read_only is True


def test_state_check_reports_not_observed_for_a_missing_file() -> None:
    observation = StateCheckProbe().run(
        "absent.db",
        _context(path="examples/does-not-exist.json"),
    )
    assert observation.result is ProbeOutcome.NOT_OBSERVED


def test_state_check_reports_skipped_when_given_no_way_to_read() -> None:
    """A probe that cannot reach its target must not report that it found nothing."""

    observation = StateCheckProbe().run("anything", _context())
    assert observation.result is ProbeOutcome.SKIPPED


def test_state_check_reports_not_observed_for_a_directory(tmp_path: Path) -> None:
    """A directory is not content; the probe says so rather than calling it an error.

    ``is_file()`` is checked before the read precisely so a missing or non-file target is a
    clean ``not_observed`` and only a read that actually raises becomes an ``error``.
    """

    observation = StateCheckProbe().run(
        "a-directory",
        _context(path=str(tmp_path)),
    )
    assert observation.result is ProbeOutcome.NOT_OBSERVED


def test_state_check_reports_error_when_the_read_raises(tmp_path: Path) -> None:
    """A read that raises is an error in the probe, not a finding about the target."""

    def unreadable() -> bytes:
        raise OSError("permission denied")

    observation = StateCheckProbe().run("bookings", _context(reader=unreadable))
    assert observation.result is ProbeOutcome.ERROR
    assert observation.detail is not None
    assert "OSError" in observation.detail


def test_state_check_uses_an_injected_reader_and_never_opens_its_own_connection() -> None:
    """The probe is handed a reader; it cannot reach a database it was not given."""

    observation = StateCheckProbe().run(
        "bookings",
        _context(reader=lambda: b"booking_17|confirmed\n"),
    )
    assert observation.result is ProbeOutcome.OBSERVED
    assert observation.payload == {"length": 21}


# --- reexecution -----------------------------------------------------------


def test_reexecution_observes_a_zero_exit_code() -> None:
    observation = ReexecutionProbe().run(
        "tests",
        _context(runner=lambda: (0, "12 passed", "")),
    )
    assert observation.result is ProbeOutcome.OBSERVED
    assert observation.verification_strength is VerificationStrength.RERUN_BY_NYX
    assert observation.payload is not None
    assert observation.payload["returncode"] == 0


def test_reexecution_reports_not_observed_on_a_non_zero_exit_code() -> None:
    """A failing re-run is a real observation, not an error in the probe."""

    observation = ReexecutionProbe().run(
        "tests",
        _context(runner=lambda: (1, "", "3 failed")),
    )
    assert observation.result is ProbeOutcome.NOT_OBSERVED
    assert observation.payload is not None
    assert observation.payload["returncode"] == 1


def test_reexecution_reports_skipped_without_a_runner() -> None:
    observation = ReexecutionProbe().run("tests", _context())
    assert observation.result is ProbeOutcome.SKIPPED


def test_reexecution_hashes_the_run_output_so_it_is_reproducible() -> None:
    observation = ReexecutionProbe().run(
        "tests",
        _context(runner=lambda: (0, "12 passed", "")),
    )
    assert observation.raw_artifact_hash == content_hash(b"0\n12 passed\n")


# --- tool call verification ------------------------------------------------


def test_tool_call_verification_matches_a_recorded_call() -> None:
    recorded = {"tool": "db.insert", "arguments": {"table": "bookings"}, "result": "ok"}
    claimed = {"tool": "db.insert", "arguments": {"table": "bookings"}}
    observation = ToolCallVerificationProbe().run(
        "the insert",
        _context(recorded_tool_call=recorded, claimed_tool_call=claimed),
    )
    assert observation.result is ProbeOutcome.OBSERVED


def test_tool_call_verification_rejects_mismatched_arguments() -> None:
    recorded = {"tool": "db.insert", "arguments": {"table": "invoices"}, "result": "ok"}
    claimed = {"tool": "db.insert", "arguments": {"table": "bookings"}}
    observation = ToolCallVerificationProbe().run(
        "the insert",
        _context(recorded_tool_call=recorded, claimed_tool_call=claimed),
    )
    assert observation.result is ProbeOutcome.NOT_OBSERVED


def test_tool_call_verification_rejects_a_call_with_no_recorded_result() -> None:
    recorded = {"tool": "db.insert", "arguments": {"table": "bookings"}}
    claimed = {"tool": "db.insert", "arguments": {"table": "bookings"}}
    observation = ToolCallVerificationProbe().run(
        "the insert",
        _context(recorded_tool_call=recorded, claimed_tool_call=claimed),
    )
    assert observation.result is ProbeOutcome.NOT_OBSERVED


def test_tool_call_verification_skips_when_either_side_is_missing() -> None:
    observation = ToolCallVerificationProbe().run("the insert", _context())
    assert observation.result is ProbeOutcome.SKIPPED


# --- side effect -----------------------------------------------------------


def test_side_effect_probe_observes_an_existing_effect() -> None:
    observation = SideEffectProbe().run(
        "the booking",
        _context(exists=lambda: {"id": "booking_17", "status": "confirmed"}),
    )
    assert observation.result is ProbeOutcome.OBSERVED


def test_side_effect_probe_reports_not_observed_for_a_missing_effect() -> None:
    observation = SideEffectProbe().run("the booking", _context(exists=lambda: None))
    assert observation.result is ProbeOutcome.NOT_OBSERVED


def test_side_effect_probe_skips_without_an_existence_check() -> None:
    observation = SideEffectProbe().run("the booking", _context())
    assert observation.result is ProbeOutcome.SKIPPED


# --- log / metric query ----------------------------------------------------


def test_log_metric_query_observes_rows() -> None:
    rows = [{"ts": "2026-10-25T00:00:00Z", "level": "error"}]
    observation = LogMetricQueryProbe().run(
        "the error window",
        _context(query=lambda since: rows),
    )
    assert observation.result is ProbeOutcome.OBSERVED
    assert observation.payload == {"rows": rows}


def test_log_metric_query_reports_not_observed_for_an_empty_window() -> None:
    observation = LogMetricQueryProbe().run(
        "the error window",
        _context(query=lambda since: []),
    )
    assert observation.result is ProbeOutcome.NOT_OBSERVED


def test_log_metric_query_skips_without_a_query_function() -> None:
    observation = LogMetricQueryProbe().run("the error window", _context())
    assert observation.result is ProbeOutcome.SKIPPED


# --- the observation a probe produces --------------------------------------


def test_probe_context_fills_in_a_collector_when_none_is_named() -> None:
    context = ProbeContext(subject="s", environment=Environment.LOCAL)
    assert context.collector is None
    assert context.with_defaults().collector == default_collector()


def test_make_observation_defaults_the_strength_from_the_probe_kind() -> None:
    observation = make_observation(
        probe=StateCheckProbe(),
        target="t",
        context=_context(),
        outcome=ProbeOutcome.OBSERVED,
        detail="seen",
    )
    assert observation.verification_strength is (
        VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE
    )


def test_make_observation_lets_a_probe_override_its_strength() -> None:
    """A probe that did less than its kind's default says so rather than overclaiming."""

    observation = make_observation(
        probe=StateCheckProbe(),
        target="t",
        context=_context(),
        outcome=ProbeOutcome.OBSERVED,
        detail="seen",
        strength=VerificationStrength.RERUN_BY_NYX,
    )
    assert observation.verification_strength is VerificationStrength.RERUN_BY_NYX


def test_make_observation_carries_the_claim_link_and_subject_version() -> None:
    observation = make_observation(
        probe=StateCheckProbe(),
        target="t",
        context=_context(),
        outcome=ProbeOutcome.OBSERVED,
        detail="seen",
    )
    assert observation.relevance_to_claim == "clm_1"
    assert observation.subject_version == "abc123"
    assert observation.subject == "booking-service"


def test_make_observation_hashes_the_artifact_it_was_given() -> None:
    observation = make_observation(
        probe=StateCheckProbe(),
        target="t",
        context=_context(),
        outcome=ProbeOutcome.OBSERVED,
        detail="seen",
        artifact=b"payload",
    )
    assert observation.raw_artifact_hash == content_hash(b"payload")


def test_an_observation_without_an_artifact_has_no_hash() -> None:
    observation = make_observation(
        probe=StateCheckProbe(),
        target="t",
        context=_context(),
        outcome=ProbeOutcome.OBSERVED,
        detail="seen",
    )
    assert observation.raw_artifact_hash is None


def test_log_metric_query_records_the_window_it_queried() -> None:
    seen: list[object] = []
    window_start = datetime(2026, 10, 25, tzinfo=UTC)

    def query(since: object) -> list[dict[str, str]]:
        seen.append(since)
        return []

    LogMetricQueryProbe().run("window", _context(query=query, window_start=window_start))
    assert seen == [window_start]


def test_probe_observations_default_to_now_when_no_collector_is_named() -> None:
    observation = make_observation(
        probe=StateCheckProbe(),
        target="t",
        context=ProbeContext(subject="s", environment=Environment.LOCAL),
        outcome=ProbeOutcome.OBSERVED,
        detail="seen",
    )
    assert observation.collector == default_collector()
    assert observation.collected_at <= utc_now()
