"""Schema-validated NYX configuration.

Safe defaults bind to ``127.0.0.1``, use a file-backed SQLite database under ``./data``, and
enable redaction. Environment variables use the ``NYX_`` prefix and an optional local ``.env``
file is read (environment variables win).

Invalid settings fail at construction with a clear Pydantic error. ``safe_effective_config``
and ``display_effective_config`` never render secret values or database passwords (SPEC §15).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal["none", "local", "api_key"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_SECRET_MARKERS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class Settings(BaseSettings):
    """Runtime configuration for the local NYX service."""

    model_config = SettingsConfigDict(
        env_prefix="NYX_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    database_url: str = "sqlite:///./data/nyx.sqlite3"
    data_dir: Path = Path("data")
    max_payload_bytes: int = Field(default=1_048_576, ge=1)
    max_batch_size: int = Field(default=100, ge=1)
    enabled_critics: list[str] = Field(default_factory=list)
    enabled_adapters: list[str] = Field(default_factory=list)
    log_level: LogLevel = "INFO"
    redaction_enabled: bool = True
    auth_mode: AuthMode = "none"
    auth_api_key: SecretStr | None = None
    llm_enabled: bool = False
    llm_provider: str | None = None
    llm_model: str | None = None
    # Conversational shell (Phase 7). All are optional: the deterministic core never needs them,
    # and `nyx chat` refuses to start with a clear message rather than guessing a provider.
    llm_protocol: Literal["openai", "anthropic"] = "openai"
    llm_base_url: str = "http://127.0.0.1:3000"
    llm_api_key: SecretStr | None = None
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=0, ge=0)
    llm_max_tool_rounds: int = Field(default=8, ge=1, le=20)
    audit_verify_on_startup: bool = True

    @model_validator(mode="after")
    def _check_safe_auth_boundary(self) -> Self:
        if self.host not in _LOOPBACK_HOSTS and self.auth_mode == "none":
            raise ValueError(
                "refusing to bind a non-loopback host with auth_mode='none'; "
                "set NYX_AUTH_MODE explicitly before exposing NYX"
            )
        if self.auth_mode == "api_key" and self.auth_api_key is None:
            raise ValueError("auth_mode='api_key' requires NYX_AUTH_API_KEY to be set")
        if self.llm_enabled and not self.llm_model:
            raise ValueError(
                "NYX_LLM_ENABLED=true requires NYX_LLM_MODEL to be set; "
                "the chat shell will not guess a model"
            )
        return self

    def safe_effective_config(self) -> dict[str, Any]:
        """Return every setting with secret values redacted."""

        data = self.model_dump()
        safe: dict[str, Any] = {}
        for key, value in data.items():
            safe[key] = _redact_setting(key, value)
        return safe

    def effective_config(self) -> dict[str, Any]:
        """Alias for ``safe_effective_config`` used by loggers and diagnostics."""

        return self.safe_effective_config()

    def display_effective_config(self) -> str:
        """Return a human-readable, secret-safe rendering of effective settings."""

        return json.dumps(self.safe_effective_config(), indent=2, sort_keys=True, default=str)


def _normalise_key(key: str) -> str:
    return key.lower().replace("-", "_").replace(" ", "_")


def _looks_secret(key: str) -> bool:
    normalised = _normalise_key(key)
    collapsed = normalised.replace("_", "")
    return any(marker in normalised for marker in _SECRET_MARKERS) or any(
        marker.replace("_", "") in collapsed for marker in _SECRET_MARKERS
    )


def redact_database_url(url: str) -> str:
    """Render a database URL without a password component."""

    parts = urlsplit(url)
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    if parts.username is not None:
        host = f"{parts.username}@{host}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _redact_setting(key: str, value: Any) -> Any:
    if isinstance(value, SecretStr):
        return "***redacted***"
    if key == "database_url" and isinstance(value, str):
        return redact_database_url(value)
    if _looks_secret(key):
        return "***redacted***"
    if isinstance(value, Path):
        return str(value)
    return value
