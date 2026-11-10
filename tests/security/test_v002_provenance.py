"""Producer identity and verification remain claims, not server authority."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import ValidationError

from nyx.ingestion.inputs import EvidenceInput

API = "/api/v1"


def _payload() -> dict[str, object]:
    return {
        "kind": "attestation",
        "title": "Producer's claimed verification",
        "description": "A producer cannot establish server verification.",
        "source_name": "untrusted-producer",
        "source_ref": "fixture://provenance/1",
        "captured_at": "2026-10-26T10:00:00Z",
        "valid_from": "2026-10-26T10:00:00Z",
        "integrity_method": "none",
        "producer_identity": {"id": "nyx-verifier", "kind": "principal"},
        "verification": {
            "result": "verified",
            "method": "producer-self-assertion",
            "verified_by": "nyx-verifier",
            "notes": "Forged producer stamp",
        },
        "relevance_note": "Unlinked provenance boundary test.",
        "limitations": ["Producer supplied this data."],
        "sensitivity": "internal",
        "access_policy_ref": "test",
        "retention_class": "standard",
        "producer_event_id": "provenance-forgery-1",
    }


def test_http_producer_identity_and_verification_are_only_claimed_metadata(
    client: TestClient,
) -> None:
    created = client.post(f"{API}/evidence", json=_payload())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["producer_identity"]["id"] == "local-operator"
    assert body["claimed_producer_identity"]["id"] == "nyx-verifier"
    assert body["verification"] is None
    assert body["claimed_verification"]["result"] == "verified"

    readback = client.get(f"{API}/evidence/{body['id']}")
    assert readback.status_code == 200, readback.text
    assert readback.json() == body


def test_replay_does_not_replace_original_provenance(service) -> None:
    first_payload = _payload()
    first = service.register_evidence(first_payload, request_id="provenance-first")
    replay_payload = _payload()
    replay_payload["producer_identity"] = {"id": "different-attacker", "kind": "principal"}
    second = service.register_evidence(replay_payload, request_id="provenance-replay")
    assert first.status == "created"
    assert second.status == "rejected"
    stored = service.get_evidence(first.record_id)
    assert stored is not None
    assert stored.producer_identity is None
    assert stored.verification is None
    assert stored.claimed_producer_identity is not None
    assert stored.claimed_producer_identity.id == "nyx-verifier"


def test_producer_dto_rejects_server_claimed_fields() -> None:
    payload = _payload()
    payload["claimed_verification"] = {"result": "verified"}
    try:
        EvidenceInput.model_validate(payload)
    except ValidationError:
        return
    raise AssertionError("producer DTO accepted a server-established metadata field")
