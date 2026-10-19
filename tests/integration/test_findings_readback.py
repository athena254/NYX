"""Regression: a persisted finding must be readable back.

Both ``GET /api/v1/findings`` and ``GET /api/v1/findings/{id}`` returned **500** because the
repository rebuilt each ``Finding`` with a hardcoded ``critic_runs=[]``, while
``Finding.critic_runs`` requires at least one entry. Every read-back failed validation.

The gap that let this through: no test read a *persisted* finding back. The composer was
tested in memory, and the write path was tested, but nothing round-tripped through the
repository -- so the write worked and the read was broken.

These tests close that gap, and double as end-to-end coverage of the C-001 mismatch path
(a completion claim for one commit, verified by evidence for a different commit).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

API = "/api/v1"


def _seed(client: TestClient, evidence_commit: str) -> dict:
    """Register a system, claim, event and evidence; return the ids and the critic outcome."""

    sys_id = client.post(
        f"{API}/systems",
        json={
            "name": "readback-sys",
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
            "producer_event_id": "rb-claim-1",
        },
    ).json()["id"]

    event_id = client.post(
        f"{API}/events",
        json={
            "source_system_id": sys_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-19T10:00:00Z",
            "correlation_id": "rb-corr-1",
            "payload": {"claim_id": claim_id},
            "producer_event_id": "rb-evt-1",
            "trust_class": "observed",
            "labels": ["readback"],
            "retention_class": "standard",
        },
    ).json()["id"]

    # EvidenceInput requires a source_system_id or source_name; without one the POST is
    # rejected, the evidence never links, and C-001 then fires for the wrong reason.
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
            "producer_event_id": "rb-ev-1",
        },
    )

    run = client.post(
        f"{API}/critics/run",
        json={"event_id": event_id, "critic_ids": ["c001-completion-vs-verification"]},
    )
    assert run.status_code == 200, run.text
    return run.json()["results"][0]


def test_mismatched_commit_produces_a_finding_that_can_be_read_back(
    client: TestClient,
) -> None:
    """The mismatch finding must be retrievable by id, with its critic run attached."""

    outcome = _seed(client, evidence_commit="def456")
    assert outcome["result"] == "finding"
    finding_id = outcome["finding_id"]
    assert finding_id, "C-001 did not produce a finding for a mismatched commit"

    single = client.get(f"{API}/findings/{finding_id}")
    assert single.status_code == 200, f"read-back by id failed: {single.text[:400]}"
    body = single.json()
    assert body["id"] == finding_id
    assert body["critic_runs"], "the finding came back with no critic runs attached"
    assert body["critic_runs"][0]["critic_id"] == "c001-completion-vs-verification"
    # A finding must separate what was observed from what was inferred.
    assert body["reasoning"]
    assert body["limitations"]


def test_findings_list_returns_the_persisted_finding(client: TestClient) -> None:
    """The list endpoint must not 500 on a store that actually contains a finding."""

    outcome = _seed(client, evidence_commit="def456")
    assert outcome["finding_id"]

    listing = client.get(f"{API}/findings")
    assert listing.status_code == 200, f"list failed: {listing.text[:400]}"
    page = listing.json()
    ids = [item["id"] for item in page["items"]]
    assert outcome["finding_id"] in ids


def test_same_commit_evidence_produces_no_finding(client: TestClient) -> None:
    """The false-positive guard: same-commit passing evidence must not be accused."""

    outcome = _seed(client, evidence_commit="abc123")

    assert outcome["result"] == "no_finding", outcome
    assert outcome["finding_id"] is None
