"""Real loopback HTTP from MCP to the protected API; no model/Telegram calls."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import Engine
from tests.unit.test_conversational_mcp import _text

from nyx.api.app import create_app
from nyx.config import Settings
from nyx.conversational import mcp_server
from nyx.conversational.client import NyxClient
from nyx.storage.db import SessionFactory


@pytest.fixture()
def protected_origin(
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    monkeypatch.setenv("NYX_CLIENT_API_KEY", "synthetic-systems-reader")
    monkeypatch.setenv("NYX_AUTH_API_KEY", "synthetic-server-owner")
    monkeypatch.setenv("NYX_LLM_API_KEY", "synthetic-wrong-provider-key")
    configured = settings.model_copy(
        update={
            "auth_mode": "api_key",
            "auth_api_key": SecretStr("synthetic-server-owner"),
            "auth_principals": [
                {
                    "principal_id": "systems-reader",
                    "api_key_env": "NYX_CLIENT_API_KEY",
                    "capabilities": ["systems.read"],
                }
            ],
            "rate_limit_enabled": False,
        }
    )
    app = create_app(configured, engine=engine, session_factory=session_factory)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "owned HTTP server failed to start"
        monkeypatch.setenv("NYX_CLIENT_BASE_URL", origin)
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "owned HTTP server did not stop"


def test_default_mcp_client_authenticates_as_its_scoped_principal(
    protected_origin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Before endpoint configuration exists, prevent accidental calls to a shared :8000.
    # This seam changes only the default origin; actual sockets and authentication run.
    def at_owned_origin(*args, **kwargs):
        kwargs.setdefault("base_url", protected_origin)
        return NyxClient(*args, **kwargs)

    monkeypatch.setattr(mcp_server, "NyxClient", at_owned_origin)
    server = mcp_server.build_server()
    allowed = json.loads(_text(asyncio.run(server.call_tool("nyx_list_systems", {}))))
    assert allowed == {"operation": "list_systems", "results": []}
    denied = json.loads(_text(asyncio.run(server.call_tool("nyx_list_findings", {}))))
    assert denied["error"] == "FORBIDDEN", denied
    assert "synthetic-systems-reader" not in json.dumps([allowed, denied])


@pytest.mark.parametrize("entrypoint", ["chat", "telegram"])
def test_cli_consumers_reach_the_same_protected_api_without_using_provider_keys(
    protected_origin: str,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    from nyx.cli import main as cli

    monkeypatch.setenv("NYX_LLM_ENABLED", "true")
    monkeypatch.setenv("NYX_LLM_MODEL", "fixture-not-invoked")
    monkeypatch.setenv("NYX_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("NYX_TELEGRAM_BOT_TOKEN", "synthetic-telegram-token")
    monkeypatch.setenv("NYX_TELEGRAM_ALLOWED_USER_IDS", "[123]")
    original_settings = cli.Settings

    def owned_settings():
        configured = original_settings()
        # Existing legacy host/port are pointed at our owned server even before
        # CLIENT_BASE_URL exists; this RED isolates absent credentials, not DNS.
        return configured.model_copy(update={"port": int(protected_origin.rsplit(":", 1)[1])})

    monkeypatch.setattr(cli, "Settings", owned_settings)

    # Construction only; any real model use is a test failure.
    class NoModel:
        def complete(self, *args, **kwargs):
            raise AssertionError("this test must not call a model")

    monkeypatch.setattr(cli, "build_client", lambda *args, **kwargs: NoModel())
    if entrypoint == "chat":
        built = cli._build_agent(actor="fixture-user")
    else:
        built = cli._build_telegram_bot()
    assert built is not None


@pytest.mark.parametrize(
    "bad_key", ["", " synthetic-key ", "synthetic-key\r\nX-Injected: yes", "synthetic-\u00e9"]
)
def test_client_refuses_malformed_header_credentials_without_echoing_them(bad_key: str) -> None:
    with pytest.raises(ValueError) as denied:
        NyxClient(api_key=SecretStr(bad_key))
    assert "API key" in str(denied.value)
    if bad_key:
        assert bad_key not in str(denied.value)


def test_no_client_key_does_not_borrow_owner_or_model_credentials(
    protected_origin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NYX_CLIENT_API_KEY")
    server = mcp_server.build_server()
    payload = json.loads(_text(asyncio.run(server.call_tool("nyx_list_systems", {}))))
    assert payload["error"] == "UNAUTHENTICATED", payload
