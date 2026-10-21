"""Validated configuration for the Git/CI adapter (phase 5, slice 5.1).

This model is deliberately **secret-free**. ``token_env`` names the environment variable that
holds the GitHub PAT; the *value* is read once by :class:`GitHubCiClient` at construction and
is never copied into this model, a log line, an exception, or a health detail. A configuration
object that could carry the token would be a configuration object that could leak it.

``from_settings`` maps the ``git_ci_*`` fields that ``Settings`` already carries (slice 5.0),
so the adapter is configured by the same environment as the rest of NYX and by nothing else.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from nyx.config import Settings


class GitCiConfig(BaseModel):
    """Validated adapter configuration. No secret field: the token is read from the environment.

    ``repository`` is ``owner/name`` and nothing else, so a typo cannot quietly retarget the
    adapter at a repository nobody meant to observe.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["github", "local_git", "fixture"]
    api_base_url: str
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    # The NAME of the env var holding the PAT; never the value. There is no `token` field by
    # design: `model_fields` is asserted not to contain one by the unit tests.
    token_env: str | None = None
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    max_runs_per_fetch: int = Field(default=10, ge=1, le=100)
    # Mirrors `evidence_allow_private_urls`; the default is the safe side, and `Settings`
    # refuses a config where the two disagree.
    allow_private_targets: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> GitCiConfig:
        """Build the adapter config from the ``git_ci_*`` fields on ``Settings``.

        The token is *not* read here: it is not a setting, and reading it here would put it in
        an object that diagnostics render. The client reads it, once, at construction.
        """

        return cls(
            provider=settings.git_ci_provider,
            api_base_url=settings.git_ci_api_base_url,
            repository=settings.git_ci_repository,
            token_env=settings.git_ci_token_env,
            timeout_seconds=settings.git_ci_timeout_seconds,
            max_runs_per_fetch=settings.git_ci_max_runs_per_fetch,
            allow_private_targets=settings.git_ci_allow_private_targets,
        )


__all__ = ["GitCiConfig"]
