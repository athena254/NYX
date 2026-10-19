"""Configuration safety tests."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from nyx.config import Settings


def test_remote_bind_without_auth_is_rejected() -> None:
    with pytest.raises(ValidationError, match="non-loopback"):
        Settings(host="0.0.0.0", auth_mode="none")  # noqa: S104 - tests the rejected value


def test_api_key_auth_requires_a_key() -> None:
    with pytest.raises(ValidationError, match="requires NYX_AUTH_API_KEY"):
        Settings(auth_mode="api_key")


def test_effective_config_never_prints_secrets() -> None:
    settings = Settings(
        auth_mode="api_key",
        auth_api_key=SecretStr("super-secret-value"),
        database_url="postgresql://user:db-password@example.invalid/nyx",
    )

    safe = settings.safe_effective_config()
    rendered = settings.display_effective_config()

    assert safe["auth_api_key"] == "***redacted***"
    assert safe["database_url"] == "postgresql://user@example.invalid/nyx"
    assert "super-secret-value" not in rendered
    assert "db-password" not in rendered
