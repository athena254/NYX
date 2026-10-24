"""Wiring for the optional model critic (Phase 6, SPEC §13).

The model critic is off unless :attr:`~nyx.config.Settings.llm_critic_enabled` is true and a
model is named. This module keeps the settings-to-critic translation in one place so the API,
the CLI, and the watcher register the identical critic: a model critic that only one entry point
knows about would make the same event produce different runs depending on how it was submitted.

The provider seam is injected (``SyncCriticTransport``); this module decides nothing about the
model's output, and registration is what makes the run visible in ``CriticRun`` provenance.
"""

from __future__ import annotations

from nyx.config import Settings
from nyx.llm.provider import LLMConfig


def llm_critic_config(settings: Settings) -> LLMConfig | None:
    """Return the provider config for the model critic, or ``None`` when it is disabled.

    Enabling the critic without a model is a configuration error rather than a silent no-op;
    ``Settings`` refuses that combination before this function is reached, and the check here
    keeps the invariant true for a caller that constructs ``Settings.model_construct()``.
    """

    if not settings.llm_critic_enabled:
        return None
    if not settings.llm_model:
        raise ValueError(
            "llm_critic_enabled=true requires a model; the model critic will not guess one"
        )
    return LLMConfig(
        protocol=settings.llm_protocol,
        base_url=settings.llm_base_url,
        api_key=(
            settings.llm_api_key.get_secret_value() if settings.llm_api_key is not None else None
        ),
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


def register_llm_critic(settings: Settings) -> bool:
    """Register c011 when the settings enable it; return whether registration happened."""

    from nyx.critics.llm_transport import build_llm_critic
    from nyx.critics.registry import register

    config = llm_critic_config(settings)
    if config is None:
        return False
    register(
        build_llm_critic(
            config,
            provider=settings.llm_provider,
            log_prompts=settings.llm_log_prompts,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    )
    return True


__all__ = ["llm_critic_config", "register_llm_critic"]
