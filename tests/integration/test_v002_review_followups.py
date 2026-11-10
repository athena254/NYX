"""Discriminating regressions for the independent citation/auth review."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from tests.integration.test_disputes import _advance_to_verification
from tests.integration.test_disputes_api import _seed

from nyx.api.app import create_app
from nyx.config import Settings
from nyx.disputes.models import VerificationJob
from nyx.disputes.service import DisputeService
from nyx.domain.base import PrincipalRef, utc_now
from nyx.domain.enums import DispositionOutcome
from nyx.domain.ids import new_critic_run_id, new_finding_id
from nyx.findings.lifecycle import CapabilityDeniedError
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.critic_runs import CriticRunRepository
from nyx.storage.repositories.findings import FindingRepository
from nyx.storage.repositories.verification_jobs import VerificationJobRepository
from nyx.storage.tables import AuditRow, Base

API = "/api/v1"


def _raw_audit(factory: SessionFactory) -> list[dict]:
    with session_scope(factory) as session:
        return [
            dict(row)
            for row in session.execute(
                select(AuditRow.__table__).order_by(AuditRow.sequence)
            ).mappings()
        ]


def _note_only_evidence(client: TestClient, original_id: str) -> str:
    evidence = client.get(f"{API}/evidence/{original_id}").json()
    for field in (
        "id",
        "schema_version",
        "content_hash",
        "claimed_producer_identity",
        "claimed_verification",
    ):
        evidence.pop(field, None)
    evidence.update(
        producer_event_id="history-only-late-report",
        source_ref="https://ci.example.invalid/history-only-report",
        relevance_to_claim=None,
        relevance_to_finding=None,
        relevance_note="A later report whose case relationship is recorded by its rebuttal.",
    )
    response = client.post(f"{API}/evidence", json=evidence)
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.parametrize("outcome", ["rejected", "inconclusive"])
def test_late_rebuttal_history_is_an_admissible_relationship_without_producer_tags(
    client: TestClient, session_factory: SessionFactory, outcome: str
) -> None:
    finding_id, _, original_evidence = _seed(client)
    with session_scope(session_factory) as session:
        _advance_to_verification(session, finding_id)
    late_id = _note_only_evidence(client, original_evidence)
    rebuttal = client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "operator",
            "argument": "The later report answers this case.",
            "evidence_ids": [late_id],
        },
    )
    assert rebuttal.status_code == 200, rebuttal.text
    before = client.get(f"{API}/findings/{finding_id}/history").json()
    assert late_id not in before["finding"]["evidence_ids"]
    response = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": outcome,
            "rationale": "Weighed the actual later case-linked report.",
            "decided_by": "operator",
            "policy_version": "review-followup",
            "counterevidence_ids": [late_id],
        },
    )
    assert response.status_code == 200, response.text
    after = client.get(f"{API}/findings/{finding_id}/history").json()
    assert after["rebuttals"] == before["rebuttals"]
    for field in ("title", "summary", "reasoning"):
        assert after["finding"][field] == before["finding"][field]
    original_links = before["finding"]["evidence_ids"]
    assert after["finding"]["evidence_ids"][: len(original_links)] == original_links
    assert late_id in after["finding"]["counterevidence_ids"]


def test_another_findings_rebuttal_is_not_a_case_relationship(
    client: TestClient, session_factory: SessionFactory
) -> None:
    finding_id, _, original_evidence = _seed(client)
    late_id = _note_only_evidence(client, original_evidence)
    other_id = new_finding_id()
    with session_scope(session_factory) as session:
        original = FindingRepository(session).get(finding_id)
        assert original is not None
        runs = [
            run.model_copy(update={"id": new_critic_run_id(), "finding_id": other_id})
            for run in original.critic_runs
        ]
        FindingRepository(session).add(
            original.model_copy(
                update={
                    "id": other_id,
                    "critic_runs": runs,
                    "dedupe_key": None,
                }
            )
        )
        for run in runs:
            CriticRunRepository(session).add(run)
    rebuttal = client.post(
        f"{API}/findings/{other_id}/rebuttals",
        json={
            "submitted_by": "operator",
            "argument": "This report belongs to the other case.",
            "evidence_ids": [late_id],
        },
    )
    assert rebuttal.status_code == 200, rebuttal.text
    before = client.get(f"{API}/findings/{finding_id}/history").json()
    audit = _raw_audit(session_factory)
    denied = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": "inconclusive",
            "rationale": "A different case is not this case.",
            "decided_by": "operator",
            "policy_version": "review-followup",
            "counterevidence_ids": [late_id],
        },
    )
    assert denied.status_code == 422, denied.text
    assert client.get(f"{API}/findings/{finding_id}/history").json() == before
    assert _raw_audit(session_factory) == audit


@contextmanager
def _role_client(
    settings: Settings,
    engine: Engine,
    factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    capabilities: list[str],
) -> Iterator[TestClient]:
    monkeypatch.setenv("NYX_REVIEW_ACTOR_KEY", "synthetic-review-actor")
    configured = settings.model_copy(
        update={
            "auth_mode": "api_key",
            "auth_api_key": SecretStr("synthetic-review-owner"),
            "auth_principals": [
                {
                    "principal_id": "review-actor",
                    "api_key_env": "NYX_REVIEW_ACTOR_KEY",
                    "capabilities": capabilities,
                }
            ],
            "rate_limit_enabled": False,
        }
    )
    app = create_app(configured, engine=engine, session_factory=factory)
    with TestClient(app, headers={"X-API-Key": "synthetic-review-owner"}) as owner:
        yield owner


def _state(session: Session) -> dict[str, list[dict]]:
    return {
        table.name: [
            dict(row)
            for row in session.execute(
                select(table).order_by(*table.primary_key.columns)
            ).mappings()
        ]
        for table in Base.metadata.sorted_tables
    }


@pytest.mark.parametrize("surface", ["service", "http"])
@pytest.mark.parametrize(
    "reference", ["valid", "unknown_evidence", "unrelated", "unknown_job", "queued_job"]
)
@pytest.mark.parametrize(
    ("capabilities", "outcome"),
    [
        (["evidence.read"], "inconclusive"),
        (["findings.disposition"], "accepted_risk"),
        (["findings.disposition"], "duplicate"),
        (["findings.accept_risk"], "inconclusive"),
        (["findings.administer"], "inconclusive"),
    ],
)
def test_requested_outcome_is_authorized_before_reference_resolution(
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    reference: str,
    capabilities: list[str],
    outcome: str,
) -> None:
    with _role_client(settings, engine, session_factory, monkeypatch, capabilities) as owner:
        finding_id, _, evidence_id = _seed(owner)
        evidence_ids = [evidence_id]
        job_ids: list[str] = []
        if reference == "unknown_evidence":
            evidence_ids = ["ev_missing_review"]
        elif reference == "unrelated":
            evidence_ids = [_note_only_evidence(owner, evidence_id)]
        elif reference == "unknown_job":
            job_ids = ["vbj_missing_review"]
        elif reference == "queued_job":
            job = VerificationJob(
                finding_id=finding_id,
                check_id="test.independent",
                requested_by="operator",
                requested_at=utc_now(),
            )
            with session_scope(session_factory) as session:
                VerificationJobRepository(session).add(job)
            job_ids = [job.id]
        body = {
            "outcome": outcome,
            "rationale": "Authorization must precede metadata lookup.",
            "decided_by": "review-actor",
            "policy_version": "review-followup",
            "counterevidence_ids": evidence_ids,
            "verification_run_ids": job_ids,
        }
        if surface == "service":
            with session_scope(session_factory) as session:
                before = _state(session)
                service = DisputeService(
                    session, capability_checker=owner.app.state.capability_checker
                )
                with pytest.raises(CapabilityDeniedError):
                    service.record_disposition(
                        finding_id=finding_id,
                        actor=PrincipalRef(id="review-actor", kind="principal"),
                        outcome=DispositionOutcome(outcome),
                        rationale=body["rationale"],
                        decided_by="review-actor",
                        policy_version="review-followup",
                        counterevidence_ids=evidence_ids,
                        verification_run_ids=job_ids,
                    )
                assert _state(session) == before
        else:
            with session_scope(session_factory) as session:
                before = _state(session)
            denied = owner.post(
                f"{API}/findings/{finding_id}/dispositions",
                json=body,
                headers={"X-API-Key": "synthetic-review-actor"},
            )
            assert denied.status_code == 403, denied.text
            assert denied.json()["code"] == "CAPABILITY_REQUIRED"
            with session_scope(session_factory) as session:
                assert _state(session) == before


@pytest.mark.parametrize(
    ("capability", "outcome"),
    [
        ("findings.accept_risk", "accepted_risk"),
        ("findings.disposition", "inconclusive"),
    ],
)
def test_specialized_authority_does_not_require_broader_disposition_powers(
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    outcome: str,
) -> None:
    with _role_client(settings, engine, session_factory, monkeypatch, [capability]) as owner:
        finding_id, _, evidence_id = _seed(owner)
        with session_scope(session_factory) as session:
            _advance_to_verification(session, finding_id)
        response = owner.post(
            f"{API}/findings/{finding_id}/dispositions",
            json={
                "outcome": outcome,
                "rationale": "This actor has precisely the relevant authority.",
                "decided_by": "forged-body-identity",
                "policy_version": "review-followup",
                "counterevidence_ids": [evidence_id],
            },
            headers={"X-API-Key": "synthetic-review-actor"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["decided_by"] == "review-actor"


@pytest.mark.parametrize("case", ["evidence", "jobs", "evidence_then_job"])
def test_mixed_valid_then_invalid_citations_leave_every_persisted_row_unchanged(
    client: TestClient, session_factory: SessionFactory, case: str
) -> None:
    finding_id, _, evidence_id = _seed(client)
    queued = client.post(
        f"{API}/findings/{finding_id}/verification-jobs",
        json={
            "check_id": "critics.rerun-linked",
            "requested_by": "operator",
        },
    )
    assert queued.status_code == 200, queued.text
    completed = client.post(f"{API}/findings/{finding_id}/verification-jobs/run")
    assert completed.status_code == 200, completed.text
    job_id = completed.json()["items"][0]["id"]
    assert completed.json()["items"][0]["status"] == "succeeded"
    evidence_ids = [evidence_id]
    job_ids = [job_id]
    if case == "evidence":
        evidence_ids.append("ev_unknown_review")
    else:
        job_ids.append("vbj_unknown_review")
    if case == "jobs":
        evidence_ids = []
    with session_scope(session_factory) as session:
        before = _state(session)
    denied = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": "inconclusive",
            "rationale": "Every citation must resolve before writes.",
            "decided_by": "operator",
            "policy_version": "review-followup",
            "counterevidence_ids": evidence_ids,
            "verification_run_ids": job_ids,
        },
    )
    assert denied.status_code == 422, denied.text
    with session_scope(session_factory) as session:
        assert _state(session) == before


def test_a_real_completed_case_job_can_be_cited_and_read_back_in_a_disposition(
    client: TestClient,
) -> None:
    finding_id, _, evidence_id = _seed(client)
    queued = client.post(
        f"{API}/findings/{finding_id}/verification-jobs",
        json={
            "check_id": "critics.rerun-linked",
            "requested_by": "operator",
        },
    )
    assert queued.status_code == 200, queued.text
    ran = client.post(f"{API}/findings/{finding_id}/verification-jobs/run")
    assert ran.status_code == 200, ran.text
    job = ran.json()["items"][0]
    assert job["status"] == "succeeded" and job["completed_at"]
    assert client.get(f"{API}/verification-jobs/{job['id']}").json()["job"] == job
    response = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": "inconclusive",
            "rationale": "Reproduction is not independent proof.",
            "decided_by": "operator",
            "policy_version": "review-followup",
            "counterevidence_ids": [evidence_id],
            "verification_run_ids": [job["id"]],
        },
    )
    assert response.status_code == 200, response.text
    history = client.get(f"{API}/findings/{finding_id}/history").json()
    assert history["current_disposition"]["verification_run_ids"] == [job["id"]]
    assert history["verification_jobs"][0] == job
