"""HTTP surface for the dispute protocol (SPEC §9.1, §5.7).

Registering a route is not the same as the route working, so these tests exercise the endpoints
end to end against a migrated database: the request bodies, the error envelope, and the state the
finding ends up in.

The capability seam is tested both ways. The application's default is deny-all, so a disposition is
refused with ``CAPABILITY_REQUIRED``; injecting an allow-all checker (the Phase 4 seam) lets one
succeed. That pair proves the seam is real and wireable rather than decorative.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from nyx.domain.base import PrincipalRef

API = "/api/v1"
_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class _AllowAllChecker:
    """Test double for the Phase-4 seam: grants every capability."""

    def has_capability(self, principal: PrincipalRef, capability: object) -> bool:
        return True


def _seed(client: TestClient, evidence_commit: str = "def456") -> tuple[str, str, str]:
    """Register a system, claim, event and evidence; run C-001; return ids."""

    sys_id = client.post(
        f"{API}/systems",
        json={
            "name": "api-dispute-sys",
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
            "producer_event_id": "a-claim-1",
        },
    ).json()["id"]

    event_id = client.post(
        f"{API}/events",
        json={
            "source_system_id": sys_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-19T10:00:00Z",
            "correlation_id": "a-corr-1",
            "payload": {"claim_id": claim_id},
            "producer_event_id": "a-evt-1",
            "trust_class": "observed",
            "labels": ["api-dispute"],
            "retention_class": "standard",
        },
    ).json()["id"]

    evidence_id = client.post(
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
            "producer_event_id": "a-ev-1",
        },
    ).json()["id"]

    run = client.post(
        f"{API}/critics/run",
        json={"event_id": event_id, "critic_ids": ["c001-completion-vs-verification"]},
    )
    assert run.status_code == 200, run.text
    finding_id = run.json()["results"][0]["finding_id"]
    assert finding_id
    return finding_id, claim_id, evidence_id


def test_a_rebuttal_can_be_submitted_and_read_back(client: TestClient) -> None:
    finding_id, _claim_id, evidence_id = _seed(client)

    response = client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "sys_api_dispute_sys",
            "argument": "The attached report covers commit abc123, not def456.",
            "evidence_ids": [evidence_id],
            "requested_outcome": "withdrawn",
        },
    )
    assert response.status_code == 200, response.text
    rebuttal = response.json()
    assert rebuttal["finding_id"] == finding_id
    assert rebuttal["requested_outcome"] == "withdrawn"

    # The finding advanced to rebuttal_pending and is otherwise unchanged.
    finding = client.get(f"{API}/findings/{finding_id}").json()
    assert finding["status"] == "rebuttal_pending"
    assert finding["title"], "the original finding title was lost"

    listed = client.get(f"{API}/findings/{finding_id}/rebuttals")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [rebuttal["id"]]


def test_a_rebuttal_citing_unknown_evidence_is_rejected_with_the_structured_envelope(
    client: TestClient,
) -> None:
    finding_id, _claim_id, _evidence_id = _seed(client)

    response = client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "sys_api_dispute_sys",
            "argument": "Cites evidence that does not exist.",
            "evidence_ids": ["ev_does_not_exist"],
        },
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "UNKNOWN_EVIDENCE_ID"
    assert body["request_id"]
    # The refusal wrote nothing.
    assert client.get(f"{API}/findings/{finding_id}/rebuttals").json()["items"] == []


def test_an_unregistered_verification_check_is_refused(client: TestClient) -> None:
    finding_id, _claim_id, _evidence_id = _seed(client)

    response = client.post(
        f"{API}/findings/{finding_id}/verification-jobs",
        json={"check_id": "tests.run-arbitrary-command", "requested_by": "principal_operator"},
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "VERIFICATION_NOT_PERMITTED"


def test_an_allowlisted_verification_check_can_be_queued_and_run(client: TestClient) -> None:
    finding_id, _claim_id, _evidence_id = _seed(client)

    queued = client.post(
        f"{API}/findings/{finding_id}/verification-jobs",
        json={"check_id": "critics.rerun-linked", "requested_by": "principal_operator"},
    )
    assert queued.status_code == 200, queued.text
    job = queued.json()["job"]
    assert job["status"] == "queued"

    ran = client.post(f"{API}/findings/{finding_id}/verification-jobs/run")
    assert ran.status_code == 200, ran.text
    results = ran.json()["items"]
    assert [item["id"] for item in results] == [job["id"]]
    assert results[0]["status"] == "succeeded"
    # The independence limitation travels with the result, so a reader cannot mistake a
    # reproduction check for independent corroboration.
    assert "not independent corroboration" in results[0]["independence"]


def test_history_returns_the_finding_and_everything_appended_to_it(client: TestClient) -> None:
    finding_id, _claim_id, evidence_id = _seed(client)

    client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "sys_api_dispute_sys",
            "argument": "A challenge.",
            "evidence_ids": [evidence_id],
        },
    )

    response = client.get(f"{API}/findings/{finding_id}/history")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["finding"]["id"] == finding_id
    assert len(body["rebuttals"]) == 1
    assert body["verification_jobs"] == []
    assert body["dispositions"] == []
    assert body["current_disposition"] is None


def test_a_disposition_is_refused_without_the_capability(client: TestClient) -> None:
    """The application default is deny-all, so no principal may change a disposition yet."""

    finding_id, _claim_id, _evidence_id = _seed(client)

    response = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": "rejected",
            "rationale": "Attempted without the disposition capability.",
            "decided_by": "principal_operator",
            "policy_version": "1.0",
        },
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "CAPABILITY_REQUIRED"


def test_a_disposition_succeeds_once_the_capability_seam_grants_it(client: TestClient) -> None:
    """Proves the seam is wireable: the same request succeeds with a granting checker."""

    finding_id, _claim_id, evidence_id = _seed(client)
    client.app.state.capability_checker = _AllowAllChecker()

    # Walk to verification_pending through the API's own rebuttal path.
    client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "sys_api_dispute_sys",
            "argument": "A challenge to answer.",
            "evidence_ids": [evidence_id],
        },
    )

    response = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": "rejected",
            "rationale": "The premise that verification was missing does not hold.",
            "decided_by": "principal_operator",
            "policy_version": "1.0",
            "counterevidence_ids": [evidence_id],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "rejected"

    history = client.get(f"{API}/findings/{finding_id}/history").json()
    assert history["finding"]["status"] == "rejected"
    assert len(history["dispositions"]) == 1
    assert history["current_disposition"]["id"] == history["dispositions"][0]["id"]


def test_an_unknown_finding_is_a_404_with_the_structured_envelope(client: TestClient) -> None:
    response = client.get(f"{API}/findings/fnd_missing/history")
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"
