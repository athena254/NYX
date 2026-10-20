"""End-to-end verification jobs: an allowlisted check re-runs against current records.

The scenario that matters is a finding which used to be true and is no longer: the mismatched-commit
finding (claim says ``abc123``, test run says ``def456``) reproduces while the bad evidence is all
there is, and stops reproducing once evidence for the claimed commit is attached.

That exercises the real path: a persisted finding, its linked critic run, the critic registry's
context builder, the allowlist, and the new ``CriticRun`` rows that verification writes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from nyx.disputes.verification import (
    CheckResult,
    VerificationContext,
    VerificationNotPermittedError,
    default_allowlist,
)
from nyx.domain.enums import VerificationOutcome
from nyx.findings.models import Finding
from nyx.storage.db import session_scope
from nyx.storage.repositories.critic_runs import CriticRunRepository
from nyx.storage.repositories.findings import FindingRepository

API = "/api/v1"
_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _seed(client: TestClient, evidence_commit: str) -> str:
    """Register a system, claim, event and evidence; return the finding id.

    ``evidence_commit`` is the commit the test evidence is actually for. When it differs from the
    claim's commit, C-001 raises a finding.
    """

    sys_id = client.post(
        f"{API}/systems",
        json={
            "name": "verify-sys",
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
            "producer_event_id": "v-claim-1",
        },
    ).json()["id"]

    event_id = client.post(
        f"{API}/events",
        json={
            "source_system_id": sys_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-19T10:00:00Z",
            "correlation_id": "v-corr-1",
            "payload": {"claim_id": claim_id},
            "producer_event_id": "v-evt-1",
            "trust_class": "observed",
            "labels": ["verify"],
            "retention_class": "standard",
        },
    ).json()["id"]

    client.post(
        f"{API}/evidence",
        json={
            "kind": "ci_test_result",
            "title": "Required integration suite passed",
            "description": f"CI run for commit {evidence_commit}: all checks passed.",
            "source_ref": "https://ci.example.invalid/run/1",
            "source_system_id": sys_id,
            "scope": {"system_id": sys_id, "artifact": "demo", "commit": evidence_commit},
            "captured_at": "2026-10-19T09:00:00Z",
            "valid_from": "2026-10-19T09:00:00Z",
            "integrity_method": "none",
            "relevance_to_claim": claim_id,
            "limitations": ["Synthetic test evidence."],
            "sensitivity": "internal",
            "access_policy_ref": "test",
            "retention_class": "standard",
            "producer_event_id": "v-ev-1",
        },
    )

    run = client.post(
        f"{API}/critics/run",
        json={"event_id": event_id, "critic_ids": ["c001-completion-vs-verification"]},
    )
    assert run.status_code == 200, run.text
    finding_id = run.json()["results"][0]["finding_id"]
    assert finding_id, "C-001 did not produce a finding"
    return finding_id


def _attach_correct_evidence(client: TestClient, finding_id: str) -> None:
    """Attach evidence for the commit the claim actually names, so C-001 stops firing."""

    finding = client.get(f"{API}/findings/{finding_id}").json()
    claim_id = finding["claim_ids"][0]
    sys_id = finding["scope"]["system_id"]

    client.post(
        f"{API}/evidence",
        json={
            "kind": "ci_test_result",
            "title": "Required integration suite passed on the claimed commit",
            "description": "CI run for commit abc123: all checks passed.",
            "source_ref": "https://ci.example.invalid/run/2",
            "source_system_id": sys_id,
            "scope": {"system_id": sys_id, "artifact": "demo", "commit": "abc123"},
            "captured_at": "2026-10-19T11:00:00Z",
            "valid_from": "2026-10-19T11:00:00Z",
            "integrity_method": "none",
            "relevance_to_claim": claim_id,
            "limitations": ["Synthetic test evidence."],
            "sensitivity": "internal",
            "access_policy_ref": "test",
            "retention_class": "standard",
            "producer_event_id": "v-ev-2",
        },
    )


def _run_check(session_factory: object, finding_id: str) -> tuple[CheckResult, Finding, list[str]]:
    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = FindingRepository(session).get(finding_id)
        assert finding is not None
        before = [run.id for run in CriticRunRepository(session).list(finding_id=finding_id)]

        result = default_allowlist().run(
            "critics.rerun-linked",
            VerificationContext(session, finding, clock=lambda: _NOW),  # type: ignore[arg-type]
        )
        after = [run.id for run in CriticRunRepository(session).list(finding_id=finding_id)]
        return result, finding, [run_id for run_id in after if run_id not in before]


def test_the_finding_reproduces_while_the_bad_evidence_is_all_there_is(
    client: TestClient, session_factory: object
) -> None:
    finding_id = _seed(client, evidence_commit="def456")

    result, finding, new_run_ids = _run_check(session_factory, finding_id)

    assert result.outcome is VerificationOutcome.SUPPORTED
    assert "reproduced" in result.result_summary
    # The re-run is recorded as its own critic run, and the original is untouched.
    assert new_run_ids, "verification wrote no new critic run"
    assert result.tool_run_refs == new_run_ids
    # A reproduction check must never present itself as independent corroboration.
    assert "not independent corroboration" in result.independence


def test_the_finding_stops_reproducing_once_correct_evidence_is_attached(
    client: TestClient, session_factory: object
) -> None:
    finding_id = _seed(client, evidence_commit="def456")
    _attach_correct_evidence(client, finding_id)

    result, _finding, new_run_ids = _run_check(session_factory, finding_id)

    assert result.outcome is VerificationOutcome.REFUTED
    assert "did not reproduce" in result.result_summary
    assert new_run_ids


def test_verification_never_composes_a_second_finding(
    client: TestClient, session_factory: object
) -> None:
    """Re-examining a finding assesses it; it does not raise another one."""

    finding_id = _seed(client, evidence_commit="def456")
    _run_check(session_factory, finding_id)

    listing = client.get(f"{API}/findings").json()
    assert len(listing["items"]) == 1


def test_an_unregistered_check_is_refused_against_a_real_finding(
    client: TestClient, session_factory: object
) -> None:
    finding_id = _seed(client, evidence_commit="def456")

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        finding = FindingRepository(session).get(finding_id)
        assert finding is not None
        try:
            default_allowlist().run(
                "tests.run-arbitrary-command",
                VerificationContext(session, finding, clock=lambda: _NOW),  # type: ignore[arg-type]
            )
        except VerificationNotPermittedError as exc:
            assert "not on the verification allowlist" in str(exc)
        else:
            raise AssertionError("an unregistered check was allowed to run")

        # The refusal wrote nothing.
        assert CriticRunRepository(session).list(finding_id=finding_id)
