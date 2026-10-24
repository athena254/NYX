"""Phase 6 wiring: the model critic is opt-in and registered from settings.

The critic is registered from the same settings the request path uses, so a process that has it
disabled never silently serves a registry that contains it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from nyx.config import Settings
from nyx.critics import registry as critic_registry
from nyx.critics.llm import CRITIC_ID, LlmCritic
from nyx.critics.llm_registration import llm_critic_config, register_llm_critic


@pytest.fixture(autouse=True)
def _isolate_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's .env or shell from deciding what these tests assert."""

    for name in (
        "NYX_LLM_CRITIC_ENABLED",
        "NYX_LLM_LOG_PROMPTS",
        "NYX_LLM_MODEL",
        "NYX_LLM_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def restore_registry() -> Iterator[None]:
    """Undo test registrations so the global critic pack is not polluted."""

    original = dict(critic_registry._REGISTRY)
    try:
        yield
    finally:
        critic_registry._REGISTRY.clear()
        critic_registry._REGISTRY.update(original)


def test_the_model_critic_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_critic_enabled is False
    assert llm_critic_config(settings) is None
    assert register_llm_critic(settings) is False
    assert CRITIC_ID not in {info.critic_id for info in critic_registry.list_critics()}


def test_enabling_without_a_model_is_refused() -> None:
    with pytest.raises(ValidationError, match="NYX_LLM_MODEL"):
        Settings(_env_file=None, llm_critic_enabled=True)


def test_the_provider_config_comes_from_the_llm_settings() -> None:
    settings = Settings(
        _env_file=None,
        llm_critic_enabled=True,
        llm_model="local-model",
        llm_protocol="anthropic",
        llm_base_url="http://127.0.0.1:4000",
        llm_timeout_seconds=42.0,
        llm_max_retries=2,
    )

    config = llm_critic_config(settings)

    assert config is not None
    assert config.model == "local-model"
    assert config.protocol == "anthropic"
    assert config.base_url == "http://127.0.0.1:4000"
    assert config.timeout_seconds == 42.0
    assert config.max_retries == 2


def test_registration_adds_the_model_critic_with_its_provenance(
    restore_registry: None,
) -> None:
    settings = Settings(
        _env_file=None,
        llm_critic_enabled=True,
        llm_model="local-model",
        llm_provider="local",
        llm_timeout_seconds=17.0,
    )

    assert register_llm_critic(settings) is True

    critic = critic_registry.get(CRITIC_ID)
    assert isinstance(critic, LlmCritic)
    assert critic.model_provider == "local"
    assert critic.model_version == "local-model"
    assert critic.log_prompts is False
    ids = [info.critic_id for info in critic_registry.list_critics()]
    assert ids.count(CRITIC_ID) == 1


def test_prompt_logging_is_a_separate_switch(restore_registry: None) -> None:
    settings = Settings(
        _env_file=None,
        llm_critic_enabled=True,
        llm_model="local-model",
        llm_log_prompts=True,
    )

    assert register_llm_critic(settings) is True

    critic = critic_registry.get(CRITIC_ID)
    assert isinstance(critic, LlmCritic)
    assert critic.log_prompts is True


def test_registration_is_idempotent(restore_registry: None) -> None:
    settings = Settings(_env_file=None, llm_critic_enabled=True, llm_model="local-model")

    assert register_llm_critic(settings) is True
    assert register_llm_critic(settings) is True

    ids = [info.critic_id for info in critic_registry.list_critics()]
    assert ids.count(CRITIC_ID) == 1
