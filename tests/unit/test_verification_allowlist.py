"""The verification allowlist (SPEC §5.7, §7 step 8).

The property under test is the one the whole module exists for: **a check that is not registered
cannot run.** SPEC §17.2 scenario 16 requires that a verification tool cannot execute an arbitrary
command supplied by an untrusted event; the allowlist is the mechanism that guarantees it, so an
unregistered id must be refused *before* anything executes.

The database-backed re-run of a critic is covered separately in
``tests/integration/test_verification_jobs.py``; these tests use a stub session because they are
about allowlist mechanics, not persistence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy.orm import Session

from nyx.disputes.models import Rebuttal
from nyx.disputes.verification import (
    CheckResult,
    VerificationAllowlist,
    VerificationContext,
    VerificationNotPermittedError,
    VerificationRunError,
    default_allowlist,
)
from nyx.domain.base import Scope
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    RebuttalRequestedOutcome,
    VerificationOutcome,
)
from nyx.findings.models import CriticRun, Finding, Reasoning, RecommendedAction

_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _finding() -> Finding:
    run = CriticRun(
        id="crn_test_1",
        critic_id="c001-completion-vs-verification",
        critic_type=CriticType.DETERMINISTIC,
        critic_version="1.0.0",
        input_refs=["evt_test_1"],
        started_at=_NOW,
        completed_at=_NOW,
        result=CriticResultStatus.FINDING,
        rationale="The completion claim names abc123 but the test run targets def456.",
    )
    return Finding(
        id="fnd_test_1",
        title="Completion claim lacks matching verification.",
        summary="The completion claim names commit abc123 but the test run targets def456.",
        category="missing_verification",
        severity="medium",
        scope=Scope(system_id="sys_test", artifact="demo", commit="abc123"),
        affected_system_ids=["sys_test"],
        evidence_ids=["ev_test_1"],
        supporting_evidence_ids=["ev_test_1"],
        reasoning=Reasoning(observations=["The claim names abc123."]),
        recommended_action=RecommendedAction(text="Run the suite against abc123."),
        critic_runs=[run],
    )


def _context(session: object) -> VerificationContext:
    """Build a context over a stub session; the checks here never touch the database."""

    return VerificationContext(
        session=cast(Session, session),
        finding=_finding(),
        clock=lambda: _NOW,
    )


class _StubCheck:
    """A registered check that returns a fixed result."""

    check_id = "test.stub"
    description = "Returns a fixed result."
    independence_note = "stub check"

    def run(self, context: VerificationContext) -> CheckResult:
        return CheckResult(
            outcome=VerificationOutcome.SUPPORTED,
            result_summary="Stub check ran.",
            independence=self.independence_note,
        )


class _RaisingCheck:
    """A registered check that blows up, to prove a failure is recorded rather than escaping."""

    check_id = "test.raises"
    description = "Always raises."
    independence_note = "stub check"

    def run(self, context: VerificationContext) -> CheckResult:
        raise RuntimeError("boom")


def test_an_unregistered_check_is_refused_without_running() -> None:
    allowlist = VerificationAllowlist()

    assert allowlist.is_allowed("anything.at.all") is False
    with pytest.raises(VerificationNotPermittedError):
        allowlist.get("anything.at.all")

    # Nothing was registered, so nothing could have executed.
    with pytest.raises(VerificationNotPermittedError):
        allowlist.run("anything.at.all", _context(object()))


def test_the_allowlist_refuses_a_check_even_when_the_caller_supplies_one() -> None:
    """An untrusted event can name a check; naming is not permission."""

    allowlist = default_allowlist()
    assert allowlist.is_allowed("critics.rerun-linked") is True

    with pytest.raises(VerificationNotPermittedError):
        allowlist.run("critics.rm-rf-everything", _context(object()))


def test_a_registered_check_runs_and_its_result_is_returned() -> None:
    allowlist = VerificationAllowlist()
    allowlist.register(_StubCheck())

    result = allowlist.run("test.stub", _context(object()))

    assert result.outcome is VerificationOutcome.SUPPORTED
    assert result.result_summary == "Stub check ran."
    # Independence is mandatory, so a check cannot silently imply corroboration.
    assert result.independence


def test_re_registering_a_check_replaces_the_previous_entry() -> None:
    allowlist = VerificationAllowlist()
    allowlist.register(_StubCheck())
    replacement = _StubCheck()
    replacement.check_id = "test.stub"
    allowlist.register(replacement)

    assert allowlist.check_ids() == ["test.stub"]


def test_a_check_that_raises_is_reported_as_a_failure_not_a_success() -> None:
    allowlist = VerificationAllowlist()
    allowlist.register(_RaisingCheck())

    with pytest.raises(VerificationRunError):
        allowlist.run("test.raises", _context(object()))


def test_the_default_allowlist_exposes_the_builtin_check() -> None:
    allowlist = default_allowlist()
    assert allowlist.check_ids() == ["critics.rerun-linked"]


def test_check_ids_are_returned_in_stable_order() -> None:
    allowlist = VerificationAllowlist()
    allowlist.register(_StubCheck())
    later = _StubCheck()
    later.check_id = "aaa.first"
    allowlist.register(later)

    assert allowlist.check_ids() == ["aaa.first", "test.stub"]


def test_rebuttal_is_importable_alongside_verification_types() -> None:
    """A guard against an accidental circular import between the dispute modules."""

    rebuttal = Rebuttal(
        id="rbt_test_1",
        finding_id="fnd_test_1",
        submitted_by="sys_test",
        submitted_at=_NOW,
        argument="The finding no longer holds.",
        evidence_ids=[],
        requested_outcome=RebuttalRequestedOutcome.REJECTED,
    )
    assert rebuttal.requested_outcome is RebuttalRequestedOutcome.REJECTED
