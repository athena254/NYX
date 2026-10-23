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
    # Measured 2026-10-21 against qwen3.5:0.8b: a reasoning model spends its budget on a
    # `reasoning` field the adapter ignores, and one turn took 2m07s -- past a 120s default.
    # 300s is one call with headroom, not a guess at how long a model "should" take.
    llm_timeout_seconds: float = Field(default=300.0, gt=0)
    llm_max_retries: int = Field(default=0, ge=0)
    llm_max_tool_rounds: int = Field(default=8, ge=1, le=20)
    # Telegram front-end (Phase 7 slice 6.4). Disabled by default; when enabled it refuses to
    # start without a token and an explicit allowlist, so a bot can never serve strangers.
    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_allowed_user_ids: list[int] = Field(default_factory=list)
    telegram_allowed_chat_ids: list[int] = Field(default_factory=list)
    telegram_require_private_chat: bool = True
    telegram_max_message_length: int = Field(default=4096, ge=1)
    telegram_api_base: str = "https://api.telegram.org"
    telegram_timeout_seconds: float = Field(default=30.0, gt=0)
    telegram_poll_timeout_seconds: int = Field(default=30, ge=1, le=300)
    telegram_rate_limit_requests: int = Field(default=20, ge=1)
    telegram_rate_limit_window_seconds: float = Field(default=60.0, gt=0)
    telegram_actor_prefix: str = "telegram:"
    audit_verify_on_startup: bool = True

    # --- Phase 4: security and operational hardening -------------------------------
    # Authentication and authorisation. `auth_principals` is a JSON list of
    # {"principal_id": str, "api_key_env": str, "capabilities": [str]}. The raw key is read from
    # the named environment variable, so a secret never lives in this configuration.
    # `auth_api_key` remains the single-operator-key case; either one satisfies auth_mode='api_key'.
    auth_principals: list[dict[str, Any]] = Field(default_factory=list)

    # API rate limiting. In-memory and per-process; a multi-process deployment needs a shared
    # store, which is a deployment concern documented in docs/operations.md.
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=120, ge=1)
    rate_limit_window_seconds: float = Field(default=60.0, gt=0)
    rate_limit_exempt_paths: list[str] = Field(
        default_factory=lambda: ["/api/v1/health", "/api/v1/ready"]
    )

    # Evidence URL and locator policy. Both default to the safe side: a URL that resolves to a
    # private target is refused, and a locator outside the data directory is refused.
    evidence_allow_private_urls: bool = False
    evidence_allow_absolute_locators: bool = False

    # Retention. Purge is destructive, so it is opt-in; legal_hold records are never purged.
    retention_purge_enabled: bool = False
    retention_ephemeral_days: int = Field(default=7, ge=1)
    retention_standard_days: int = Field(default=90, ge=1)
    retention_extended_days: int = Field(default=365, ge=1)
    retention_derived_metadata_days: int = Field(default=730, ge=1)

    # --- Phase 5: optional adapters (ADR 0008) ----------------------------------
    # Every adapter is off by default. Enabling one is an explicit deployment decision,
    # never a prerequisite for core NYX: with no adapter named in `enabled_adapters` the
    # service behaves exactly as it did before Phase 5.
    #
    # The Git/CI token is NOT a setting. `git_ci_token_env` names the environment variable
    # that holds it, and the value is read once at client construction -- so a token never
    # enters effective configuration, a log line, or a health detail.
    git_ci_provider: Literal["github", "local_git", "fixture"] = "github"
    git_ci_api_base_url: str = "https://api.github.com"
    git_ci_repository: str = ""
    git_ci_token_env: str | None = None
    git_ci_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    git_ci_max_runs_per_fetch: int = Field(default=10, ge=1, le=100)
    # Must agree with `evidence_allow_private_urls`. Two switches that disagree about what
    # is fetchable is how an SSRF guard and an adapter end up with different answers.
    git_ci_allow_private_targets: bool = False

    # --- ADR 0009: unattended operation ----------------------------------------
    # Off by default and never a prerequisite: with watch disabled, `nyx watch` refuses with a
    # named-variable message rather than watching nothing and reporting success.
    watch_enabled: bool = False
    # A cycle is idempotent (the ingestion gateway deduplicates by identity), so re-running an
    # unchanged ref produces no new rows. That is what makes a timer safe.
    watch_interval_seconds: float = Field(default=300.0, gt=0, le=86_400)
    # The git ref the watcher observes. "main" by default; an empty value is refused, because
    # a watcher with no target is silently watching nothing.
    watch_ref: str = "main"

    @model_validator(mode="after")
    def _check_safe_auth_boundary(self) -> Self:
        if self.host not in _LOOPBACK_HOSTS and self.auth_mode == "none":
            raise ValueError(
                "refusing to bind a non-loopback host with auth_mode='none'; "
                "set NYX_AUTH_MODE explicitly before exposing NYX"
            )
        if self.auth_mode == "api_key" and self.auth_api_key is None and not self.auth_principals:
            raise ValueError(
                "auth_mode='api_key' requires NYX_AUTH_API_KEY or NYX_AUTH_PRINCIPALS to be set; "
                "NYX refuses to expose an authenticated surface with no principal"
            )
        for principal in self.auth_principals:
            if not isinstance(principal, dict) or not principal.get("principal_id"):
                raise ValueError(
                    "each NYX_AUTH_PRINCIPALS entry must be an object with a non-empty "
                    "'principal_id' key"
                )
            if not principal.get("api_key_env"):
                raise ValueError(
                    f"NYX_AUTH_PRINCIPALS entry {principal.get('principal_id')!r} must name the "
                    "environment variable holding its key via 'api_key_env'"
                )
            if not principal.get("capabilities"):
                raise ValueError(
                    f"NYX_AUTH_PRINCIPALS entry {principal.get('principal_id')!r} must list at "
                    "least one capability"
                )
        if self.llm_enabled and not self.llm_model:
            raise ValueError(
                "NYX_LLM_ENABLED=true requires NYX_LLM_MODEL to be set; "
                "the chat shell will not guess a model"
            )
        if self.telegram_enabled:
            # Fail closed, and say which variable is missing. A bot with no allowlist is a bot
            # that answers anyone who finds it, which is the one thing it must never do.
            if self.telegram_bot_token is None:
                raise ValueError(
                    "NYX_TELEGRAM_ENABLED=true requires NYX_TELEGRAM_BOT_TOKEN to be set"
                )
            if not self.telegram_allowed_user_ids:
                raise ValueError(
                    "NYX_TELEGRAM_ENABLED=true requires NYX_TELEGRAM_ALLOWED_USER_IDS "
                    "to list at least one Telegram user id; the bot refuses to serve strangers"
                )
        return self

    @model_validator(mode="after")
    def _check_adapter_configuration(self) -> Self:
        """Fail closed on a Git/CI adapter that is switched on but cannot work.

        These are misconfigurations, not degraded states: an adapter with no target
        repository has nothing to observe, and a GitHub adapter with no token source
        cannot read a private repository. Refusing at startup names the variable instead
        of surfacing later as an empty observation set.
        """

        if "git-ci" not in self.enabled_adapters:
            return self
        if not self.git_ci_repository:
            raise ValueError(
                "NYX_ENABLED_ADAPTERS names 'git-ci' but NYX_GIT_CI_REPOSITORY is empty; "
                "an adapter with no target repository has nothing to observe"
            )
        if self.git_ci_provider == "github" and self.git_ci_token_env is None:
            raise ValueError(
                "NYX_ENABLED_ADAPTERS names 'git-ci' with provider 'github' but "
                "NYX_GIT_CI_TOKEN_ENV is not set; a GitHub adapter with no token source "
                "cannot read a private repository, so this is a misconfiguration rather "
                "than a degraded adapter"
            )
        if self.git_ci_allow_private_targets and not self.evidence_allow_private_urls:
            raise ValueError(
                "NYX_GIT_CI_ALLOW_PRIVATE_TARGETS=true requires "
                "NYX_EVIDENCE_ALLOW_PRIVATE_URLS=true; the adapter and the SSRF guard "
                "must agree about which targets are fetchable"
            )
        return self

    @model_validator(mode="after")
    def _check_watch_configuration(self) -> Self:
        """Fail closed on a watcher that is switched on but has nothing to watch.

        A watcher with no enabled adapter, or with no target ref, is not a degraded state -- it
        is a configuration error that would otherwise surface as a process that runs, fetches
        nothing, and reports success. Refusing at startup names the variable.
        """

        if not self.watch_enabled:
            return self
        if not self.watch_ref.strip():
            raise ValueError(
                "NYX_WATCH_ENABLED=true but NYX_WATCH_REF is empty; a watcher with no target "
                "ref would fetch nothing and report success"
            )
        if not self.enabled_adapters:
            raise ValueError(
                "NYX_WATCH_ENABLED=true but NYX_ENABLED_ADAPTERS is empty; a watcher with no "
                "enabled adapter has nothing to observe"
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
