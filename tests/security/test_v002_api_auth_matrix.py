"""Every protected API operation must reject missing/invalid credentials before dispatch."""

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine, func, select

from nyx.api.app import create_app
from nyx.config import Settings
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.tables import AuditRow, SystemRow


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "synthetic-wrong-key"}])
def test_authentication_covers_every_protected_operation(
    settings: Settings, engine: Engine, session_factory: SessionFactory, headers: dict[str, str]
) -> None:
    authenticated = settings.model_copy(
        update={
            "auth_mode": "api_key",
            "auth_api_key": SecretStr("synthetic-v002-owner-key"),
            "rate_limit_enabled": False,
        }
    )
    app = create_app(authenticated, engine=engine, session_factory=session_factory)
    checked: list[str] = []
    with TestClient(app) as client:
        # OpenAPI enumerates mounted operations across FastAPI's router wrappers as well
        # as directly mounted APIRoute instances.
        for route_path, operations in app.openapi()["paths"].items():
            if not route_path.startswith("/api/v1/"):
                continue
            if route_path in {"/api/v1/health", "/api/v1/ready"}:
                continue
            path = re.sub(r"\{[^}]+\}", "missing_record", route_path)
            for method in sorted(set(operations) & {"get", "post", "put", "patch", "delete"}):
                response = client.request(method.upper(), path, headers=headers, json={})
                checked.append(f"{method.upper()} {route_path}")
                assert response.status_code == 401, (
                    f"{method.upper()} {route_path} bypassed authentication: {response.status_code}"
                )
        assert checked, "An empty route inventory is not authentication coverage"
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code == 200
    with session_scope(session_factory) as session:
        assert session.scalar(select(func.count()).select_from(SystemRow)) == 0
        assert session.scalar(select(func.count()).select_from(AuditRow)) == 0


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/api/v1/systems", None),
        (
            "POST",
            "/api/v1/systems",
            {
                "name": "unauthorized-write",
                "kind": "agent",
                "environment": "development",
                "owner": "test",
                "adapter": "http",
            },
        ),
        ("POST", "/api/v1/events", {}),
        ("POST", "/api/v1/critics/run", {"event_id": "evt_missing"}),
        ("POST", "/api/v1/evidence", {}),
    ],
)
def test_evidence_reader_cannot_gain_other_api_powers(
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    payload: dict | None,
) -> None:
    monkeypatch.setenv("NYX_MATRIX_READER_KEY", "synthetic-reader-key")
    restricted = settings.model_copy(
        update={
            "auth_mode": "api_key",
            "auth_api_key": None,
            "auth_principals": [
                {
                    "principal_id": "evidence-reader",
                    "api_key_env": "NYX_MATRIX_READER_KEY",
                    "capabilities": ["evidence.read"],
                }
            ],
            "rate_limit_enabled": False,
        }
    )
    app = create_app(restricted, engine=engine, session_factory=session_factory)
    with TestClient(app) as client:
        response = client.request(
            method, path, headers={"X-API-Key": "synthetic-reader-key"}, json=payload
        )
    assert response.status_code == 403, response.text
    with session_scope(session_factory) as session:
        assert session.scalar(select(func.count()).select_from(SystemRow)) == 0


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.100.37", "::"])  # noqa: S104 - refusal inputs; no bind
def test_local_auth_mode_cannot_expose_anonymous_owner_over_a_network(host: str) -> None:
    with pytest.raises(ValidationError, match="non-loopback"):
        Settings(_env_file=None, host=host, auth_mode="local")


def test_owner_credential_reaches_every_declared_operation(
    settings: Settings, engine: Engine, session_factory: SessionFactory
) -> None:
    configured = settings.model_copy(
        update={
            "auth_mode": "api_key",
            "auth_api_key": SecretStr("synthetic-v002-owner-key"),
            "rate_limit_enabled": False,
        }
    )
    app = create_app(configured, engine=engine, session_factory=session_factory)
    with TestClient(app) as client:
        for route_path, operations in app.openapi()["paths"].items():
            path = re.sub(r"\{[^}]+\}", "missing_record", route_path)
            for method in sorted(set(operations) & {"get", "post", "put", "patch", "delete"}):
                result = client.request(
                    method.upper(),
                    path,
                    headers={"X-API-Key": "synthetic-v002-owner-key"},
                    json={},
                )
                assert result.status_code not in {401, 403}, (
                    f"Owner was denied for {method.upper()} {route_path}: {result.text}"
                )
