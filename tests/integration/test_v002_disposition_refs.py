"""Citation guards accept real late evidence and reject invalid jobs atomically."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from tests.integration.test_disputes import _advance_to_verification
from tests.integration.test_disputes_api import _seed

from nyx.api.app import create_app
from nyx.config import Settings
from nyx.disputes.models import VerificationJob
from nyx.disputes.verification import (
    CheckResult,
    RerunLinkedCriticsCheck,
    VerificationAllowlist,
    VerificationContext,
)
from nyx.domain.enums import VerificationJobStatus
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.verification_jobs import VerificationJobRepository

API = "/api/v1"
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def test_late_rebuttal_counterevidence_remains_admissible(
    client: TestClient, session_factory: SessionFactory
) -> None:
    finding_id, claim_id, original_evidence_id = _seed(client)
    with session_scope(session_factory) as session:
        _advance_to_verification(session, finding_id)
    evidence = client.get(f"{API}/evidence/{original_evidence_id}").json()
    for field in (
        "id",
        "content_hash",
        "schema_version",
        "claimed_producer_identity",
        "claimed_verification",
    ):
        evidence.pop(field, None)
    evidence.update(
        producer_event_id="late-independent-report",
        source_ref="https://ci.example.invalid/late-report",
        relevance_to_claim=claim_id,
        relevance_to_finding=None,
        description="A later report correcting the original evidence.",
    )
    registered = client.post(f"{API}/evidence", json=evidence)
    assert registered.status_code == 201, registered.text
    late_id = registered.json()["id"]
    rebuttal = client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "operator",
            "argument": "The later claim-linked report is counterevidence.",
            "evidence_ids": [late_id],
        },
    )
    assert rebuttal.status_code == 200, rebuttal.text
    before = client.get(f"{API}/findings/{finding_id}/history").json()
    assert late_id not in before["finding"]["evidence_ids"]
    assert before["finding"]["status"] == "verification_pending"
    verdict = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": "rejected",
            "rationale": "The real late report refutes the original finding.",
            "decided_by": "operator",
            "policy_version": "v002-test",
            "counterevidence_ids": [late_id],
        },
    )
    assert verdict.status_code == 200, verdict.text
    after = client.get(f"{API}/findings/{finding_id}/history").json()
    assert after["finding"]["status"] == "rejected"
    assert after["rebuttals"] == before["rebuttals"]


@pytest.mark.parametrize("case", ["unknown", "other_finding", "queued", "failed", "unfinished"])
def test_invalid_verification_reference_is_atomic(
    client: TestClient, session_factory: SessionFactory, case: str
) -> None:
    finding_id, _, _ = _seed(client)
    job_id = "vjob_nonexistent"
    if case != "unknown":
        job = VerificationJob(
            finding_id="fnd_other" if case == "other_finding" else finding_id,
            check_id="test.independent",
            requested_by="operator",
            requested_at=NOW,
            status=(
                VerificationJobStatus.QUEUED
                if case == "queued"
                else VerificationJobStatus.FAILED
                if case == "failed"
                else VerificationJobStatus.SUCCEEDED
            ),
            completed_at=None if case in {"queued", "unfinished"} else NOW,
        )
        with session_scope(session_factory) as session:
            VerificationJobRepository(session).add(job)
        job_id = job.id
    before = client.get(f"{API}/findings/{finding_id}/history").json()
    response = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": "inconclusive",
            "rationale": "An invalid citation must not move the lifecycle.",
            "decided_by": "operator",
            "policy_version": "v002-test",
            "verification_run_ids": [job_id],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VERIFICATION_FAILED"
    assert client.get(f"{API}/findings/{finding_id}/history").json() == before


def test_completed_verification_is_readable_after_execution(client: TestClient) -> None:
    finding_id, _, _ = _seed(client)
    queued = client.post(
        f"{API}/findings/{finding_id}/verification-jobs",
        json={"check_id": "critics.rerun-linked", "requested_by": "operator"},
    )
    assert queued.status_code == 200, queued.text
    job_id = queued.json()["job"]["id"]
    ran = client.post(f"{API}/findings/{finding_id}/verification-jobs/run")
    assert ran.status_code == 200, ran.text
    completed = ran.json()["items"][0]
    assert completed["status"] == "succeeded"
    readback = client.get(f"{API}/verification-jobs/{job_id}")
    assert readback.status_code == 200, readback.text
    assert readback.json()["job"] == completed
    repeated = client.post(f"{API}/findings/{finding_id}/verification-jobs/run")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["items"] == []


def test_a_stale_duplicate_queue_snapshot_does_not_execute_twice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    finding_id, _, _ = _seed(client)
    queued = client.post(
        f"{API}/findings/{finding_id}/verification-jobs",
        json={"check_id": "critics.rerun-linked", "requested_by": "operator"},
    )
    assert queued.status_code == 200, queued.text
    original = VerificationJobRepository.list_queued

    def stale_snapshot(
        repository: VerificationJobRepository, *, limit: int = 100
    ) -> list[VerificationJob]:
        return list(original(repository, limit=limit)) * 2

    monkeypatch.setattr(VerificationJobRepository, "list_queued", stale_snapshot)
    ran = client.post(f"{API}/findings/{finding_id}/verification-jobs/run")
    assert ran.status_code == 200, ran.text
    assert len(ran.json()["items"]) == 1


@pytest.mark.parametrize(
    ("suffix", "identity_field", "payload"),
    [
        ("rebuttals", "submitted_by", {"argument": "A genuine authenticated challenge."}),
        ("verification-jobs", "requested_by", {"check_id": "critics.rerun-linked"}),
        (
            "dispositions",
            "decided_by",
            {
                "outcome": "inconclusive",
                "rationale": "An authenticated verdict.",
                "policy_version": "v002-test",
            },
        ),
    ],
)
def test_authenticated_mutation_records_real_principal_not_body_identity(
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
    suffix: str,
    identity_field: str,
    payload: dict[str, str],
) -> None:
    configured = settings.model_copy(
        update={
            "auth_mode": "api_key",
            "auth_api_key": SecretStr("synthetic-attribution-owner"),
            "rate_limit_enabled": False,
        }
    )
    app = create_app(configured, engine=engine, session_factory=session_factory)
    with TestClient(app, headers={"X-API-Key": "synthetic-attribution-owner"}) as owner:
        finding_id, _, evidence_id = _seed(owner)
        request_body: dict[str, object] = {**payload, identity_field: "forged-other-user"}
        if suffix == "dispositions":
            request_body["counterevidence_ids"] = [evidence_id]
        response = owner.post(f"{API}/findings/{finding_id}/{suffix}", json=request_body)
        assert response.status_code == 200, response.text
        body = response.json()["job"] if suffix == "verification-jobs" else response.json()
        assert body[identity_field] == "operator"
        history = owner.get(f"{API}/findings/{finding_id}/history").json()
        collection = "verification_jobs" if suffix == "verification-jobs" else suffix
        assert history[collection][0][identity_field] == "operator"


class _FailingCheck(RerunLinkedCriticsCheck):
    def run(self, context: VerificationContext) -> CheckResult:
        raise RuntimeError("Controlled check failure")


@pytest.mark.parametrize("outcome", ["failed", "refused"])
def test_unsuccessful_verification_is_readable_after_execution(
    client: TestClient, outcome: str
) -> None:
    finding_id, _, _ = _seed(client)
    queued = client.post(
        f"{API}/findings/{finding_id}/verification-jobs",
        json={"check_id": "critics.rerun-linked", "requested_by": "operator"},
    )
    assert queued.status_code == 200, queued.text
    job_id = queued.json()["job"]["id"]
    replacement = VerificationAllowlist()
    if outcome == "failed":
        replacement.register(_FailingCheck())
    client.app.state.verification_allowlist = replacement
    ran = client.post(f"{API}/findings/{finding_id}/verification-jobs/run")
    assert ran.status_code == 200, ran.text
    completed = ran.json()["items"][0]
    assert completed["status"] == outcome
    assert completed["failure_details"]
    readback = client.get(f"{API}/verification-jobs/{job_id}")
    assert readback.json()["job"] == completed
    assert client.post(f"{API}/findings/{finding_id}/verification-jobs/run").json()["items"] == []
