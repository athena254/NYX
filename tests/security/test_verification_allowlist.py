"""Scenario 16: a verification tool cannot execute an arbitrary command from an untrusted event.

The safety property is narrow and absolute: **a check that is not registered cannot run.** An
event can name a check; naming one that does not exist is refused before anything is executed, so
untrusted input never reaches a command line.

Also asserted here:

* ``VerificationContext`` structurally carries no payload field, so a check has nothing to
  interpret even if it wanted to;
* a registered check that raises produces a ``failed`` job and leaves the finding unchanged -- a
  buggy check can never look like a passed one;
* a regression guard that scans ``src/`` for ``subprocess``, ``os.system``, ``shell=True``,
  ``eval(`` and ``exec(`` and fails the build if any appears. This is the real control for "no
  arbitrary shell execution".
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nyx.disputes.models import VerificationJobStatus
from nyx.disputes.service import DisputeService
from nyx.disputes.verification import (
    CheckResult,
    VerificationAllowlist,
    VerificationChecker,
    VerificationContext,
    VerificationNotPermittedError,
    VerificationRunError,
    default_allowlist,
)
from nyx.domain.base import utc_now
from nyx.domain.enums import VerificationOutcome
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.findings import FindingRepository

API = "/api/v1"
_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPOSITORY_ROOT / "src"

#: Patterns that would let an event, finding, or model output reach a command line. ``src/`` must
#: contain none of them; the allowlist below exists so a legitimate, reviewed exception is
#: explicit rather than silent.
_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "subprocess",
    "os.system",
    "shell=True",
    "eval(",
    "exec(",
)
_ALLOWED_FILES: dict[str, tuple[str, ...]] = {}


@dataclasses.dataclass(frozen=True)
class _DummyFinding:
    """Stand-in for a Finding in tests that never touch the database."""

    id: str = "fnd_dummy"


def _context() -> VerificationContext:
    return VerificationContext(session=None, finding=_DummyFinding(), clock=utc_now)  # type: ignore[arg-type]


class _ExplodingCheck:
    """A registered check that raises, so the failure path is exercised on purpose."""

    check_id = "tests.explode"
    description = "Always raises, to prove a failure is recorded rather than swallowed."
    independence_note = "test double; not an independent method"

    def run(self, context: VerificationContext) -> CheckResult:
        raise RuntimeError("this check is broken on purpose")


# --------------------------------------------------------------------------- allowlist


def test_an_unregistered_check_is_refused_before_anything_executes() -> None:
    allowlist = default_allowlist()
    executed: list[str] = []

    class _Spy:
        check_id = "tests.spy"
        description = "Records that it ran."
        independence_note = "test double; not an independent method"

        def run(self, context: VerificationContext) -> CheckResult:
            executed.append(self.check_id)
            return CheckResult(
                outcome=VerificationOutcome.SUPPORTED,
                result_summary="ran",
                independence="test double; not an independent method",
            )

    allowlist.register(_Spy())

    assert allowlist.is_allowed("tests.spy") is True
    assert allowlist.is_allowed("tests.run-arbitrary-command") is False

    with pytest.raises(VerificationNotPermittedError) as excinfo:
        allowlist.run("tests.run-arbitrary-command", _context())

    assert excinfo.value.code.value == "VERIFICATION_NOT_PERMITTED"
    # The refusal happened before anything ran: the spy never executed.
    assert executed == []
    assert "tests.run-arbitrary-command" in str(excinfo.value)


def test_an_event_payload_cannot_smuggle_a_check_id_through_the_context() -> None:
    """The context carries no payload, so there is nothing for a check to be steered by."""

    context = _context()

    field_names = {field.name for field in dataclasses.fields(VerificationContext)}
    assert field_names == {"session", "finding", "clock"}
    assert "payload" not in field_names
    assert "event" not in field_names
    assert "event_payload" not in field_names
    # The instance exposes no attribute an untrusted payload could have been bound to.
    assert not hasattr(context, "payload")
    assert not any(name.endswith("payload") for name in vars(context))


def test_a_registered_check_that_raises_is_converted_to_a_run_error() -> None:
    allowlist = VerificationAllowlist()
    allowlist.register(_ExplodingCheck())

    with pytest.raises(VerificationRunError) as excinfo:
        allowlist.run("tests.explode", _context())

    assert excinfo.value.code.value == "VERIFICATION_FAILED"


# --------------------------------------------------------------------------- failure is an outcome


def _seed_finding(client: TestClient) -> str:
    """Register a system, claim, event and evidence; return the finding id."""

    sys_id = client.post(
        f"{API}/systems",
        json={
            "name": "allowlist-sys",
            "kind": "agent",
            "environment": "development",
            "owner": "test",
            "adapter": "http",
        },
    ).json()["id"]

    claim_id = client.post(
        f"{API}/claims",
        json={
            "statement": "All required tests pass for commit abc123.",
            "claimant_system_id": sys_id,
            "claim_type": "completion",
            "scope": {"system_id": sys_id, "artifact": "demo", "commit": "abc123"},
            "producer_event_id": "al-claim-1",
        },
    ).json()["id"]

    event_id = client.post(
        f"{API}/events",
        json={
            "source_system_id": sys_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-19T10:00:00Z",
            "correlation_id": "al-corr-1",
            "payload": {"claim_id": claim_id},
            "producer_event_id": "al-evt-1",
            "trust_class": "observed",
            "labels": ["allowlist"],
            "retention_class": "standard",
        },
    ).json()["id"]

    client.post(
        f"{API}/evidence",
        json={
            "kind": "ci_test_result",
            "title": "Required integration suite passed",
            "description": "CI run for commit def456: all checks passed.",
            "source_ref": "https://ci.example.invalid/run/1",
            "source_system_id": sys_id,
            "scope": {"system_id": sys_id, "artifact": "demo", "commit": "def456"},
            "captured_at": "2026-10-19T09:00:00Z",
            "valid_from": "2026-10-19T09:00:00Z",
            "integrity_method": "none",
            "relevance_to_claim": claim_id,
            "limitations": ["Synthetic test evidence."],
            "sensitivity": "internal",
            "access_policy_ref": "test",
            "retention_class": "standard",
            "producer_event_id": "al-ev-1",
        },
    )

    run = client.post(
        f"{API}/critics/run",
        json={"event_id": event_id, "critic_ids": ["c001-completion-vs-verification"]},
    )
    assert run.status_code == 200, run.text
    return run.json()["results"][0]["finding_id"]


def test_a_failing_check_produces_a_failed_job_and_leaves_the_finding_unchanged(
    client: TestClient, session_factory: SessionFactory
) -> None:
    finding_id = _seed_finding(client)

    allowlist = VerificationAllowlist()
    allowlist.register(_ExplodingCheck())

    with session_scope(session_factory) as session:
        service = DisputeService(session, allowlist=allowlist, clock=lambda: _NOW)
        job = service.request_verification(
            finding_id=finding_id,
            check_id="tests.explode",
            requested_by="principal_operator",
        )
        assert job.status is VerificationJobStatus.QUEUED

        before = FindingRepository(session).get(finding_id)
        completed = service.run_pending_verification(finding_id=finding_id)
        after = FindingRepository(session).get(finding_id)

    assert before is not None and after is not None
    job_row = next(item for item in completed if item.id == job.id)

    # The failure is recorded as a failure, never rewritten into a success.
    assert job_row.status is VerificationJobStatus.FAILED
    assert job_row.outcome is None
    assert job_row.failure_details
    assert "RuntimeError" in job_row.failure_details

    # The finding is untouched: only an authorised disposition moves it.
    assert after.model_dump() == before.model_dump()


# --------------------------------------------------------------------------- regression guard


def _scan_tree(
    root: Path, *, allow: dict[str, tuple[str, ...]] | None = None
) -> dict[str, list[tuple[int, str, str]]]:
    """Return ``{relative_path: [(line, pattern, text)]}`` for forbidden patterns under ``root``.

    ``allow`` overrides the module-level allowlist, so a test can prove the mechanism works
    without mutating global state.
    """

    allowed = _ALLOWED_FILES if allow is None else allow
    offenders: dict[str, list[tuple[int, str, str]]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = str(path.relative_to(root))
        if relative in allowed:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for pattern in _FORBIDDEN_PATTERNS:
                if pattern in line:
                    offenders.setdefault(relative, []).append((lineno, pattern, line.strip()))
    return offenders


def test_src_contains_no_arbitrary_command_execution() -> None:
    offenders = _scan_tree(_SRC_ROOT)

    assert offenders == {}, (
        "arbitrary-execution patterns found in src/; if one is legitimate, add it to "
        f"_ALLOWED_FILES with a reason: {offenders}"
    )


def test_the_regression_guard_actually_detects_a_violation(tmp_path: Path) -> None:
    """The guard must fail when a violation exists, not merely pass on a clean tree."""

    (tmp_path / "evil.py").write_text(
        "import subprocess\nsubprocess.run(['sh', '-c', user_input], shell=True)\n",
        encoding="utf-8",
    )
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")

    offenders = _scan_tree(tmp_path)

    assert "evil.py" in offenders
    assert {pattern for _, pattern, _ in offenders["evil.py"]} >= {"subprocess", "shell=True"}
    assert "clean.py" not in offenders


def test_the_allowlist_is_explicit_not_silent(tmp_path: Path) -> None:
    """An allowed file is skipped by the scanner, so an exception is a decision, not an accident."""

    (tmp_path / "tooling.py").write_text("import subprocess  # reviewed\n", encoding="utf-8")

    assert _scan_tree(tmp_path) == {
        "tooling.py": [(1, "subprocess", "import subprocess  # reviewed")]
    }
    assert _scan_tree(tmp_path, allow={"tooling.py": ("subprocess",)}) == {}


def test_the_allowlist_contains_no_unused_entries() -> None:
    """Every allowlisted path must still exist, or the allowlist is drift."""

    for relative in _ALLOWED_FILES:
        assert (_SRC_ROOT / relative).exists(), f"allowlisted path {relative!r} does not exist"


def test_verification_checker_protocol_is_structural() -> None:
    """A check must satisfy the protocol; the allowlist is a set of code, not of names."""

    checker: VerificationChecker = _ExplodingCheck()  # type: ignore[assignment]

    assert checker.check_id == "tests.explode"
    assert checker.description
    assert checker.independence_note
