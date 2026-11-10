"""Supplemental privacy boundaries for otherwise valid derived records."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.security.test_v002_derived_privacy import (
    MARKER,
    OWNER_KEY,
    READER_KEY,
    _bearer,
    _create_pair,
    _settings,
)

from nyx.api.app import create_app
from nyx.api.evidence_projection import finding_depends_on_unreadable_evidence
from nyx.auth.principal import AuthenticatedPrincipal
from nyx.domain.base import PrincipalRef
from nyx.findings.lifecycle import Capability
from nyx.storage.db import create_engine_from_settings, create_session_factory, session_scope
from nyx.storage.repositories.findings import FindingRepository
from nyx.storage.tables import EvidenceRow, FindingRow


@pytest.mark.parametrize("field", ["unknowns", "limitations", "remediation_hint"])
def test_all_derived_free_text_respects_its_evidence_policy(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setenv("NYX_DERIVED_PRIVACY_READER_KEY", READER_KEY)
    settings = _settings(database, tmp_path)
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    app = create_app(settings, engine=engine, session_factory=factory)
    try:
        with TestClient(app) as client:
            finding_id, _, _ = _create_pair(client)
            value = [MARKER] if field in {"unknowns", "limitations"} else MARKER
            with session_scope(factory) as session:
                row = session.get(FindingRow, finding_id)
                assert row is not None
                setattr(row, field, value)
            response = client.get(f"/api/v1/findings/{finding_id}", headers=_bearer(READER_KEY))
            assert response.status_code == 200, response.text
            assert MARKER not in response.text
            authorized = client.get(f"/api/v1/findings/{finding_id}", headers=_bearer(OWNER_KEY))
            assert authorized.json()[field] == value
    finally:
        engine.dispose()


def test_a_non_evidence_principal_cannot_read_ordinary_evidence_via_a_finding(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NYX_DERIVED_PRIVACY_READER_KEY", READER_KEY)
    settings = _settings(database, tmp_path)
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    app = create_app(settings, engine=engine, session_factory=factory)
    try:
        with TestClient(app) as client:
            finding_id, _, evidence_ids = _create_pair(client)
            principal = AuthenticatedPrincipal(
                id=PrincipalRef(id="decision-only", kind="principal"),
                capabilities=frozenset({Capability.DISPOSITION}),
                auth_mode="api_key",
            )
            with session_scope(factory) as session:
                for evidence_id in evidence_ids:
                    evidence = session.get(EvidenceRow, evidence_id)
                    assert evidence is not None
                    evidence.access_policy_ref = "evidence:internal"
                session.flush()
                finding = FindingRepository(session).get(finding_id)
                assert finding is not None
                assert finding_depends_on_unreadable_evidence(session, finding, principal)
    finally:
        engine.dispose()


def test_restricted_derived_input_hashes_are_not_disclosed(
    database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NYX_DERIVED_PRIVACY_READER_KEY", READER_KEY)
    settings = _settings(database, tmp_path)
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    app = create_app(settings, engine=engine, session_factory=factory)
    try:
        with TestClient(app) as client:
            finding_id, _, _ = _create_pair(client)
            owner = client.get(f"/api/v1/findings/{finding_id}", headers=_bearer(OWNER_KEY))
            assert owner.json()["critic_runs"][0]["input_hashes"]
            reader = client.get(f"/api/v1/findings/{finding_id}", headers=_bearer(READER_KEY))
            assert reader.json()["critic_runs"][0]["input_hashes"] == []
    finally:
        engine.dispose()
