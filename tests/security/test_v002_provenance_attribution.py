"""Supplemental replay controls and actual request audit attribution."""

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from tests.security.test_v002_provenance import _payload

from nyx.api.app import create_app
from nyx.config import Settings
from nyx.domain.base import PrincipalRef
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.audit import AuditRepository


def test_evidence_audit_records_the_authenticated_submitter_not_body_or_gateway(
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
) -> None:
    configured = settings.model_copy(
        update={
            "auth_mode": "api_key",
            "auth_api_key": SecretStr("synthetic-provenance-owner"),
            "rate_limit_enabled": False,
        }
    )
    app = create_app(configured, engine=engine, session_factory=session_factory)
    with TestClient(app, headers={"X-API-Key": "synthetic-provenance-owner"}) as api:
        response = api.post("/api/v1/evidence", json=_payload())
        assert response.status_code == 201, response.text
        evidence = response.json()
    with session_scope(session_factory) as session:
        entries = AuditRepository(session).list(target_type="evidence", target_id=evidence["id"])
    assert len(entries) == 1
    assert entries[0].action == "evidence.registered"
    assert entries[0].actor == PrincipalRef.model_validate(evidence["producer_identity"])
    assert entries[0].actor.id != "nyx-verifier"


def test_identical_and_conflicting_replays_preserve_both_claims_and_original_submitter(
    service: IngestionService,
) -> None:
    first = service.register_evidence(
        _payload(),
        request_id="first",
        submitted_by=PrincipalRef(id="submitter-a", kind="principal"),
    )
    assert first.status == "created"
    original = service.get_evidence(first.record_id)
    assert original is not None
    repeated = service.register_evidence(
        _payload(),
        request_id="identical",
        submitted_by=PrincipalRef(id="submitter-b", kind="principal"),
    )
    assert repeated.status == "duplicate"
    changed = _payload()
    changed["producer_identity"] = {"id": "different-forged-identity", "kind": "principal"}
    changed["verification"] = {"result": "verified", "method": "different-assertion"}
    conflicted = service.register_evidence(changed, request_id="conflicting")
    assert conflicted.status == "rejected"
    stored = service.get_evidence(first.record_id)
    assert stored == original
    assert stored.producer_identity.id == "submitter-a"
    assert stored.verification is None
    assert stored.claimed_producer_identity.id == "nyx-verifier"
    assert stored.claimed_verification.method == "producer-self-assertion"
