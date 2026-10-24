"""The single place an adapter registry is constructed (ADR 0008, ADR 0009).

Before ADR 0009 the only constructor lived inside ``nyx doctor``, which built the registry purely
to print one line. A watcher needs the same set from the same settings, and a second copy would be
a second source of truth that drifts — the watcher would observe a different set of adapters than
the doctor reports, and neither would be obviously wrong.

The factory is deliberately free of configuration: it takes a :class:`~nyx.config.Settings` and
applies ``enabled_adapters`` as the deployment filter. Callers decide which settings they have.
"""

from __future__ import annotations

from nyx.adapters.agent_transcript.config import AgentTranscriptConfig
from nyx.adapters.agent_transcript.hermes import HermesAdapter
from nyx.adapters.base import AdapterRegistry
from nyx.adapters.git_ci.adapter import GitCiAdapter
from nyx.adapters.git_ci.config import GitCiConfig
from nyx.config import Settings


def build_adapter_registry(settings: Settings | None = None) -> AdapterRegistry:
    """Return the adapter registry for this deployment.

    Every registered adapter is constructed, then filtered by ``enabled_adapters``. A disabled
    adapter is registered but not enabled, which is what lets ``nyx doctor`` report
    "registered but disabled" honestly (ADR 0008) and lets the watcher skip it without
    constructing a client it will never call.

    Construction failures are the caller's to handle: a misconfigured adapter raises
    :class:`~pydantic.ValidationError` here, because "the adapter is installed but its repository
    is not set" is a configuration fact the caller needs verbatim.
    """

    effective = settings or Settings()
    enabled = set(effective.enabled_adapters)
    registry = AdapterRegistry(is_enabled=lambda adapter_id: adapter_id in enabled)
    registry.register(GitCiAdapter(GitCiConfig.from_settings(effective)))
    # Each agent-transcript adapter shares one config shape but its own auto-detect store, so the
    # config is built per adapter from that adapter's default path. Adding an agent is one import
    # and one register() call here -- the shared base does the rest (ADR 0010).
    hermes_config = AgentTranscriptConfig.from_settings(
        effective, default_db=HermesAdapter.default_db_path()
    )
    registry.register(HermesAdapter(hermes_config))
    return registry
