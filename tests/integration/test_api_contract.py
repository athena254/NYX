"""HTTP contract for the Phase 1 API subset (SPEC §9.1, docs/api-error-conventions.md)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

FIXTURES = Path(__file__).resolve().parents[2] / "examples" / "ghost-factory"
API = "/api/v1"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _event(producer_event_id: str) -> dict[str, Any]:
    document = _fixture("event-completion-claim.json")
    document.pop("id", None)
    document["producer_event_id"] = producer_event_id
    return document


def test_register_and_list_systems(client: TestClient) -> None:
    created = client.post(f"{API}/systems", json=_fixture("system-manifest.json"))
    assert created.status_code == 201
    body = created.json()
    assert body["id"] == "sys_ghost_factory_demo"
    assert created.headers["X-Request-Id"]

    listed = client.get(f"{API}/systems")
    assert listed.status_code == 200
    page = listed.json()
    assert [item["id"] for item in page["items"]] == ["sys_ghost_factory_demo"]
    assert page["next_cursor"] is None


def test_list_systems_rejects_an_invalid_cursor(client: TestClient) -> None:
    response = client.get(f"{API}/systems", params={"cursor": "not-a-cursor"})

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"


def test_single_event_ingest_and_replay(client: TestClient) -> None:
    registered = client.post(f"{API}/systems", json=_fixture("system-manifest.json"))
    assert registered.status_code == 201

    document = _event("api-event-1")

    created = client.post(f"{API}/events", json=document)
    assert created.status_code == 201
    event_id = created.json()["id"]

    fetched = client.get(f"{API}/events/{event_id}")
    assert fetched.status_code == 200
    stored = fetched.json()
    assert stored["id"] == event_id
    source_system_id = stored["source_system_id"]
    assert source_system_id == document["source_system_id"]

    traced = client.get(f"{API}/systems")
    assert traced.status_code == 200
    assert source_system_id in {item["id"] for item in traced.json()["items"]}

    replay = client.post(f"{API}/events", json=document)
    assert replay.status_code == 409
    envelope = replay.json()
    assert envelope["code"] == "DUPLICATE_EVENT"
    assert event_id in envelope["message"]
    assert envelope["request_id"]


def test_missing_records_return_structured_not_found(client: TestClient) -> None:
    for path in ("/events/evt_missing", "/claims/clm_missing", "/evidence/ev_missing"):
        response = client.get(f"{API}{path}")
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"


def test_batch_is_per_item(client: TestClient) -> None:
    good = _event("api-batch-good")
    bad = copy.deepcopy(_event("api-batch-bad"))
    del bad["occurred_at"]

    response = client.post(f"{API}/events/batch", json={"events": [good, bad]})

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["status"] == "created"
    assert body["results"][0]["record_id"]
    assert body["results"][1]["status"] == "rejected"
    assert body["results"][1]["error"]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert body["request_id"]


def test_claim_and_evidence_ingest(client: TestClient) -> None:
    claim = client.post(f"{API}/claims", json=_fixture("claim-completion-abc123.json"))
    assert claim.status_code == 201
    assert claim.json()["id"] == "clm_ghost_0001"

    fetched_claim = client.get(f"{API}/claims/clm_ghost_0001")
    assert fetched_claim.status_code == 200
    assert fetched_claim.json()["statement"].startswith("All required tests pass")

    evidence = client.post(f"{API}/evidence", json=_fixture("evidence-test-def456.json"))
    assert evidence.status_code == 201
    assert evidence.json()["id"] == "ev_ghost_def456"

    fetched_evidence = client.get(f"{API}/evidence/ev_ghost_def456")
    assert fetched_evidence.status_code == 200
    assert fetched_evidence.json()["content_hash"].startswith("sha256:")


def test_error_envelope_shape_for_validation_failure(client: TestClient) -> None:
    response = client.post(f"{API}/systems", json={"name": "Bad Name"})

    assert response.status_code == 422
    body = response.json()
    assert set(body) >= {"code", "message", "request_id"}
    assert body["code"] == "SCHEMA_VALIDATION_FAILED"
    assert body["request_id"] == response.headers["X-Request-Id"]
    assert isinstance(body.get("field_details"), list)


def test_not_found_uses_the_envelope(client: TestClient) -> None:
    response = client.get(f"{API}/does-not-exist")

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_health_and_readiness(client: TestClient) -> None:
    health = client.get(f"{API}/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    ready = client.get(f"{API}/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
