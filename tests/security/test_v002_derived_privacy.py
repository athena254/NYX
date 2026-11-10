"""Derived API responses must respect the access policy of their source evidence."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nyx.api.app import create_app
from nyx.config import Settings
from nyx.storage.db import create_engine_from_settings, create_session_factory

API = "/api/v1"
OWNER_KEY = "derived-privacy-owner-key"
READER_KEY = "derived-privacy-reader-key"
MARKER = "cobalt-otter-signal-internal-review"


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _settings(database: Path, data_dir: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        data_dir=data_dir,
        auth_mode="api_key",
        auth_api_key=OWNER_KEY,
        auth_principals=[
            {
                "principal_id": "evidence-reader",
                "api_key_env": "NYX_DERIVED_PRIVACY_READER_KEY",
                "capabilities": [
                    "evidence.read",
                    "findings.read",
                    "critics.run",
                    "verification.read",
                ],
            }
        ],
    )


def _create_pair(client: TestClient) -> tuple[str, str, list[str]]:
    headers = _bearer(OWNER_KEY)
    system = client.post(
        f"{API}/systems",
        headers=headers,
        json={
            "name": "derived-privacy-system",
            "kind": "agent",
            "environment": "development",
            "owner": "test",
            "adapter": "http",
        },
    )
    assert system.status_code == 201, system.text
    system_id = system.json()["id"]

    claim = client.post(
        f"{API}/claims",
        headers=headers,
        json={
            "statement": "The verification suite is healthy.",
            "claimant_system_id": system_id,
            "claim_type": "completion",
            "scope": {
                "system_id": system_id,
                "artifact": "nyx-demo",
                "commit": "commit-current",
                "environment": "test",
            },
            "producer_event_id": "derived-privacy-claim",
        },
    )
    assert claim.status_code == 201, claim.text
    claim_id = claim.json()["id"]

    event = client.post(
        f"{API}/events",
        headers=headers,
        json={
            "source_system_id": system_id,
            "event_type": "verification.completed",
            "occurred_at": "2026-10-19T10:00:00Z",
            "correlation_id": "derived-privacy-correlation",
            "payload": {"claim_id": claim_id},
            "producer_event_id": "derived-privacy-event",
            "trust_class": "observed",
            "labels": ["privacy-regression"],
            "retention_class": "standard",
        },
    )
    assert event.status_code == 201, event.text

    evidence_ids: list[str] = []
    for suffix, outcome, time in (
        ("pass", "PASS", "2026-10-19T09:00:00Z"),
        ("fail", "FAIL", "2026-10-19T11:00:00Z"),
    ):
        evidence = client.post(
            f"{API}/evidence",
            headers=headers,
            json={
                "kind": "ci_test_result",
                "title": f"Integration validation {MARKER}",
                "description": f"{outcome} for probe {MARKER}.",
                "source_ref": f"https://ci.example.invalid/run/{suffix}",
                "source_system_id": system_id,
                "scope": {
                    "system_id": system_id,
                    "artifact": "nyx-demo",
                    "commit": f"commit-{suffix}",
                    "environment": "test",
                },
                "captured_at": time,
                "valid_from": time,
                "integrity_method": "none",
                "relevance_to_claim": claim_id,
                "limitations": ["synthetic test input"],
                "sensitivity": "internal",
                "access_policy_ref": "evidence:restricted",
                "retention_class": "standard",
                "producer_event_id": f"derived-privacy-evidence-{suffix}",
            },
        )
        assert evidence.status_code == 201, evidence.text
        evidence_ids.append(evidence.json()["id"])

    run = client.post(
        f"{API}/critics/run",
        headers=headers,
        json={"event_id": event.json()["id"], "critic_ids": ["c007-repeated-failure-regression"]},
    )
    assert run.status_code == 200, run.text
    finding_id = run.json()["results"][0]["finding_id"]
    assert finding_id, run.text
    return finding_id, event.json()["id"], evidence_ids


def test_c007_derived_finding_detail_does_not_disclose_restricted_input(
    database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NYX_DERIVED_PRIVACY_READER_KEY", READER_KEY)
    settings = _settings(database, tmp_path)
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    app = create_app(settings, engine=engine, session_factory=factory)
    try:
        with TestClient(app) as client:
            finding_id, event_id, evidence_ids = _create_pair(client)
            reader = _bearer(READER_KEY)

            surfaces = [
                client.get(f"{API}/findings/{finding_id}", headers=reader),
                client.get(f"{API}/findings", headers=reader),
                client.post(
                    f"{API}/critics/run",
                    headers=reader,
                    json={"event_id": event_id, "critic_ids": ["c007-repeated-failure-regression"]},
                ),
                client.get(f"{API}/findings/{finding_id}/history", headers=reader),
            ]
            assert all(response.status_code == 200 for response in surfaces), [
                response.text for response in surfaces
            ]
            assert all(MARKER not in response.text for response in surfaces)

            rebuttal = client.post(
                f"{API}/findings/{finding_id}/rebuttals",
                headers=_bearer(OWNER_KEY),
                json={
                    "submitted_by": "operator",
                    "argument": f"Review note {MARKER}.",
                    "evidence_ids": [evidence_ids[1]],
                },
            )
            assert rebuttal.status_code == 200, rebuttal.text
            assert MARKER in rebuttal.text
            queued = client.post(
                f"{API}/findings/{finding_id}/verification-jobs",
                headers=_bearer(OWNER_KEY),
                json={"check_id": "critics.rerun-linked", "requested_by": "operator"},
            )
            assert queued.status_code == 200, queued.text
            job_id = queued.json()["job"]["id"]
            ran = client.post(
                f"{API}/findings/{finding_id}/verification-jobs/run",
                headers=_bearer(OWNER_KEY),
            )
            assert ran.status_code == 200, ran.text

            dispute_surfaces = [
                client.get(f"{API}/findings/{finding_id}/rebuttals", headers=reader),
                client.get(f"{API}/findings/{finding_id}/verification-jobs", headers=reader),
                client.get(f"{API}/verification-jobs/{job_id}", headers=reader),
                client.get(f"{API}/findings/{finding_id}/history", headers=reader),
            ]
            assert all(response.status_code == 200 for response in dispute_surfaces), [
                response.text for response in dispute_surfaces
            ]
            assert all(MARKER not in response.text for response in dispute_surfaces)

            authorized = client.get(f"{API}/findings/{finding_id}", headers=_bearer(OWNER_KEY))
            assert authorized.status_code == 200, authorized.text
            assert MARKER in authorized.text
            evidence = client.get(f"{API}/evidence/{evidence_ids[1]}", headers=_bearer(OWNER_KEY))
            assert evidence.status_code == 200, evidence.text
            assert MARKER in evidence.json()["title"]
            assert MARKER in evidence.json()["description"]
    finally:
        engine.dispose()
