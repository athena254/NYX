"""Acceptance scenario 13: capability-gated evidence access.

These tests drive a real application over HTTP with ``auth_mode='api_key'`` and two
principals: one that may read restricted evidence and one that may only read ordinary
evidence. They also guard the local-operator path (``auth_mode='none'`` on loopback)
that the rest of the suite and ``scripts/probe_live_api.py`` depend on.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nyx.api.app import create_app
from nyx.config import Settings
from nyx.storage.db import create_engine_from_settings, create_session_factory

API = "/api/v1"
FULL_KEY = "test-full-capability-key"
READ_KEY = "test-basic-read-key"
RESTRICTED_CONTENT = "restricted-content-must-not-leak"


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@contextmanager
def _client(settings: Settings) -> Iterator[TestClient]:
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    app = create_app(settings, engine=engine, session_factory=factory)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        engine.dispose()


def _api_settings(
    database: Path,
    data_dir: Path,
    *,
    auth_mode: str = "api_key",
    principals: list[dict[str, Any]] | None = None,
) -> Settings:
    return Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        data_dir=data_dir,
        auth_mode=auth_mode,  # type: ignore[arg-type]
        auth_principals=principals or [],
    )


def _two_principals() -> list[dict[str, Any]]:
    return [
        {
            "principal_id": "full-reader",
            "api_key_env": "NYX_TEST_FULL_KEY",
            "capabilities": ["evidence.read", "evidence.restricted"],
        },
        {
            "principal_id": "basic-reader",
            "api_key_env": "NYX_TEST_READ_KEY",
            "capabilities": ["evidence.read"],
        },
    ]


@pytest.fixture()
def api_client(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("NYX_TEST_FULL_KEY", FULL_KEY)
    monkeypatch.setenv("NYX_TEST_READ_KEY", READ_KEY)
    with _client(_api_settings(database, tmp_path, principals=_two_principals())) as client:
        yield client


def _evidence_payload(
    *,
    sensitivity: str,
    access_policy_ref: str,
    producer_event_id: str,
    relevance_to_claim: str = "clm_auth_test",
    description: str = "Evidence description.",
    commit: str | None = None,
) -> dict[str, Any]:
    scope: dict[str, Any] | None = None
    if commit is not None:
        scope = {"system_id": "sys_auth_test", "artifact": "demo", "commit": commit}
    return {
        "kind": "ci_test_result",
        "title": "Evidence under test",
        "description": description,
        "source_ref": "https://ci.example.invalid/run/1",
        "source_system_id": "sys_auth_test",
        "scope": scope,
        "captured_at": "2026-10-19T09:00:00Z",
        "valid_from": "2026-10-19T09:00:00Z",
        "integrity_method": "none",
        "relevance_to_claim": relevance_to_claim,
        "limitations": ["Synthetic test evidence."],
        "sensitivity": sensitivity,
        "access_policy_ref": access_policy_ref,
        "retention_class": "standard",
        "producer_event_id": producer_event_id,
    }


def _register(
    client: TestClient,
    *,
    key: str,
    sensitivity: str,
    access_policy_ref: str,
    producer_event_id: str,
    description: str = "Evidence description.",
    relevance_to_claim: str = "clm_auth_test",
    commit: str | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"{API}/evidence",
        headers=_bearer(key),
        json=_evidence_payload(
            sensitivity=sensitivity,
            access_policy_ref=access_policy_ref,
            producer_event_id=producer_event_id,
            description=description,
            relevance_to_claim=relevance_to_claim,
            commit=commit,
        ),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _seed_finding(client: TestClient, key: str, evidence_commit: str) -> tuple[str, str]:
    headers = _bearer(key)
    sys_id = client.post(
        f"{API}/systems",
        json={
            "name": "auth-evidence-sys",
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
            "producer_event_id": "auth-claim-1",
        },
    ).json()["id"]

    event_id = client.post(
        f"{API}/events",
        json={
            "source_system_id": sys_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-19T10:00:00Z",
            "correlation_id": "auth-corr-1",
            "payload": {"claim_id": claim_id},
            "producer_event_id": "auth-evt-1",
            "trust_class": "observed",
            "labels": ["auth"],
            "retention_class": "standard",
        },
    ).json()["id"]

    evidence = _register(
        client,
        key=key,
        sensitivity="restricted",
        access_policy_ref="evidence:restricted",
        producer_event_id="auth-ev-1",
        description=f"CI run for commit {evidence_commit}. {RESTRICTED_CONTENT}",
        relevance_to_claim=claim_id,
        commit=evidence_commit,
    )

    run = client.post(
        f"{API}/critics/run",
        json={"event_id": event_id, "critic_ids": ["c001-completion-vs-verification"]},
        headers=headers,
    )
    assert run.status_code == 200, run.text
    finding_id = run.json()["results"][0]["finding_id"]
    assert finding_id
    return finding_id, evidence["id"]


def test_a_principal_with_the_capability_reads_restricted_evidence(
    api_client: TestClient,
) -> None:
    evidence = _register(
        api_client,
        key=FULL_KEY,
        sensitivity="restricted",
        access_policy_ref="evidence:restricted",
        producer_event_id="pos-ev-1",
        description=RESTRICTED_CONTENT,
    )

    response = api_client.get(f"{API}/evidence/{evidence['id']}", headers=_bearer(FULL_KEY))

    assert response.status_code == 200, response.text
    assert response.json()["description"] == RESTRICTED_CONTENT
    assert response.json()["sensitivity"] == "restricted"


def test_a_principal_without_the_capability_is_denied_with_no_content(
    api_client: TestClient,
) -> None:
    evidence = _register(
        api_client,
        key=FULL_KEY,
        sensitivity="restricted",
        access_policy_ref="evidence:restricted",
        producer_event_id="neg-ev-1",
        description=RESTRICTED_CONTENT,
    )

    response = api_client.get(f"{API}/evidence/{evidence['id']}", headers=_bearer(READ_KEY))

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "EVIDENCE_ACCESS_DENIED"
    assert RESTRICTED_CONTENT not in response.text
    assert "description" not in response.json()


def test_no_credential_in_api_key_mode_is_unauthenticated(api_client: TestClient) -> None:
    evidence = _register(
        api_client,
        key=FULL_KEY,
        sensitivity="restricted",
        access_policy_ref="evidence:restricted",
        producer_event_id="noauth-ev-1",
        description=RESTRICTED_CONTENT,
    )

    response = api_client.get(f"{API}/evidence/{evidence['id']}")

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert RESTRICTED_CONTENT not in response.text


def test_confidential_evidence_is_readable_with_evidence_read_alone(
    api_client: TestClient,
) -> None:
    evidence = _register(
        api_client,
        key=FULL_KEY,
        sensitivity="confidential",
        access_policy_ref="policy:evidence-internal-read",
        producer_event_id="conf-ev-1",
    )

    response = api_client.get(f"{API}/evidence/{evidence['id']}", headers=_bearer(READ_KEY))

    assert response.status_code == 200, response.text
    assert response.json()["id"] == evidence["id"]


def test_an_unknown_evidence_access_policy_is_rejected_at_registration(
    api_client: TestClient,
) -> None:
    response = api_client.post(
        f"{API}/evidence",
        headers=_bearer(FULL_KEY),
        json=_evidence_payload(
            sensitivity="internal",
            access_policy_ref="evidence:not-a-policy",
            producer_event_id="bad-policy-ev-1",
        ),
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "INVALID_REQUEST"
    assert body["field_details"][0]["field"] == "/access_policy_ref"


def test_finding_omits_restricted_payload_but_keeps_id_and_classification(
    api_client: TestClient,
) -> None:
    finding_id, evidence_id = _seed_finding(api_client, FULL_KEY, "def456")

    response = api_client.get(f"{API}/findings/{finding_id}", headers=_bearer(READ_KEY))

    assert response.status_code == 200, response.text
    body = response.json()
    assert evidence_id in body["evidence_ids"]
    assert RESTRICTED_CONTENT not in response.text
    assert any(
        evidence_id in limitation and "restricted" in limitation
        for limitation in body["limitations"]
    )

    full = api_client.get(f"{API}/findings/{finding_id}", headers=_bearer(FULL_KEY))
    assert full.status_code == 200, full.text
    assert not any(
        evidence_id in limitation and "restricted" in limitation
        for limitation in full.json()["limitations"]
    )


def test_auth_mode_none_on_loopback_still_reads_restricted_evidence(
    database: Path, tmp_path: Path
) -> None:
    with _client(_api_settings(database, tmp_path, auth_mode="none")) as client:
        evidence = _register(
            client,
            key="",
            sensitivity="restricted",
            access_policy_ref="evidence:restricted",
            producer_event_id="local-ev-1",
            description=RESTRICTED_CONTENT,
        )

        response = client.get(f"{API}/evidence/{evidence['id']}")

    assert response.status_code == 200, response.text
    assert response.json()["description"] == RESTRICTED_CONTENT
