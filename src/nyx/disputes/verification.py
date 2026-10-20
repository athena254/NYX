"""Independent verification: an allowlist of checks, and the jobs that run them (SPEC §5.7, §7).

The safety property this module exists to provide is narrow and absolute: **a check that is not
registered cannot run.** SPEC §17.2 scenario 16 requires that a verification tool cannot execute
an arbitrary command supplied by an untrusted event. The allowlist is the mechanism: an event can
name a check, but naming one that does not exist is refused before anything is executed, so
untrusted input never reaches a command line.

Independence is recorded, not implied
-------------------------------------
Re-running the critic that raised a finding does not corroborate it -- the evaluator shares the
original's blind spot. Every check therefore returns an ``independence`` statement, and
:class:`VerificationJob` stores it as a queryable column. Where no independent method exists the
check says so, because a reader who cannot tell the difference will assume corroboration that was
never established.

Failure is an outcome, not a corruption
---------------------------------------
A check that raises, times out, or is unavailable produces a ``failed`` or ``inconclusive`` job. It
never produces a ``succeeded`` job, and it never mutates the finding. The finding's status changes
only through the lifecycle machine, driven by an authorised disposition.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from nyx.critics.registry import rerun_for_verification
from nyx.domain.base import Identifier, utc_now
from nyx.domain.enums import VerificationOutcome
from nyx.error_codes import ErrorCode
from nyx.findings.models import Finding


class VerificationError(Exception):
    """Base class for verification failures. Carries a stable ``code``."""

    code: ErrorCode

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.detail = message


class VerificationNotPermittedError(VerificationError):
    """The requested check is not on the allowlist, so it must not run."""

    code = ErrorCode.VERIFICATION_NOT_PERMITTED


class VerificationRunError(VerificationError):
    """A registered check could not produce a usable result."""

    code = ErrorCode.VERIFICATION_FAILED


@dataclass(frozen=True)
class CheckResult:
    """What one allowlisted check established.

    ``independence`` is required, not optional: a check that cannot state how it relates to the
    originating critic has not told the reader whether its result is corroboration or repetition.
    """

    outcome: VerificationOutcome
    result_summary: str
    independence: str
    evidence_ids: list[Identifier] = field(default_factory=list)
    tool_run_refs: list[str] = field(default_factory=list)
    failure_details: str | None = None


@dataclass(frozen=True)
class VerificationContext:
    """What an allowlisted check may see.

    Deliberately narrow: a check gets the session, the finding under examination, and a clock. It
    does not get an event payload to interpret, so it cannot be steered by untrusted producer data.
    """

    session: Session
    finding: Finding
    clock: Callable[[], datetime] = utc_now


@runtime_checkable
class VerificationChecker(Protocol):
    """The structural contract every allowlisted check implements."""

    check_id: str
    description: str
    independence_note: str

    def run(self, context: VerificationContext) -> CheckResult:
        """Run the check and return what it established."""
        ...


class VerificationAllowlist:
    """The explicit set of checks permitted to run.

    There is no discovery and no configuration: a check is code, registered here. An unregistered id
    is refused with :class:`VerificationNotPermittedError` and nothing is executed.
    """

    def __init__(self) -> None:
        self._checks: dict[str, VerificationChecker] = {}

    def register(self, check: VerificationChecker) -> None:
        """Register a check. Re-registering the same id replaces the previous entry."""

        self._checks[check.check_id] = check

    def is_allowed(self, check_id: str) -> bool:
        """Return whether ``check_id`` may run."""

        return check_id in self._checks

    def check_ids(self) -> list[str]:
        """Return the allowed check ids in stable order."""

        return sorted(self._checks)

    def get(self, check_id: str) -> VerificationChecker:
        """Return the registered check, or raise if it is not allowlisted."""

        try:
            return self._checks[check_id]
        except KeyError:
            raise VerificationNotPermittedError(
                f"check {check_id!r} is not on the verification allowlist; "
                f"allowed checks: {sorted(self._checks)}"
            ) from None

    def run(self, check_id: str, context: VerificationContext) -> CheckResult:
        """Run an allowlisted check.

        Raises :class:`VerificationNotPermittedError` before executing anything when the check is
        not registered. A check that raises is converted to a failed result rather than allowed to
        escape, so a buggy check cannot corrupt the finding.
        """

        check = self.get(check_id)
        try:
            return check.run(context)
        except VerificationError:
            raise
        except Exception as exc:  # noqa: BLE001 - record the failure, never corrupt the finding
            raise VerificationRunError(
                f"check {check_id!r} failed to produce a usable result: {type(exc).__name__}"
            ) from exc


class RerunLinkedCriticsCheck:
    """Re-run the critics that produced the finding against current records.

    This is a *reproduction* check, not an independent one: the same critic re-examines the same
    records. It answers "does the finding still hold?", which is materially weaker than "does an
    independent method agree?". The independence note says so, and it is stored with every job.
    """

    check_id = "critics.rerun-linked"
    description = "Re-run the critics that produced the finding against current records."
    independence_note = (
        "re-runs the same critic that raised the finding; this is a reproduction check, not "
        "independent corroboration"
    )

    def run(self, context: VerificationContext) -> CheckResult:
        outcomes = rerun_for_verification(
            context.session, finding=context.finding, clock=context.clock
        )
        run_ids = [outcome.run_id for outcome in outcomes]

        failures = [item for item in outcomes if item.result.value == "error"]
        inconclusive = [item for item in outcomes if item.result.value == "inconclusive"]
        findings = [item for item in outcomes if item.result.value == "finding"]
        clean = [item for item in outcomes if item.result.value == "no_finding"]

        if failures:
            outcome = VerificationOutcome.INCONCLUSIVE
            summary = (
                f"{len(failures)} of {len(outcomes)} critic re-runs errored, so the finding "
                "could not be re-examined."
            )
        elif findings:
            outcome = VerificationOutcome.SUPPORTED
            summary = (
                f"The finding reproduced: {len(findings)} of {len(outcomes)} critic re-runs "
                "returned a finding against current records."
            )
        elif clean and not inconclusive:
            outcome = VerificationOutcome.REFUTED
            summary = (
                f"The finding did not reproduce: {len(clean)} of {len(outcomes)} critic re-runs "
                "returned no finding against current records."
            )
        else:
            outcome = VerificationOutcome.INCONCLUSIVE
            summary = (
                "The critic re-runs were inconclusive, so the finding is neither confirmed nor "
                "refuted."
            )

        return CheckResult(
            outcome=outcome,
            result_summary=summary,
            independence=self.independence_note,
            tool_run_refs=run_ids,
        )


def default_allowlist() -> VerificationAllowlist:
    """Return an allowlist pre-loaded with the built-in checks.

    Checks are registered here rather than discovered, so the set of things NYX will execute is
    visible in one place and an unregistered check is refused rather than run by accident.
    """

    allowlist = VerificationAllowlist()
    allowlist.register(RerunLinkedCriticsCheck())
    return allowlist
