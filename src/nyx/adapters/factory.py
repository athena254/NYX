"""The single place an adapter registry is constructed (ADR 0008, ADR 0009).

Before ADR 0009 the only constructor lived inside ``nyx doctor``, which built the registry purely
to print one line. A watcher needs the same set from the same settings, and a second copy would be
a second source of truth that drifts — the watcher would observe a different set of adapters than
the doctor reports, and neither would be obviously wrong.

The factory is deliberately free of configuration: it takes a :class:`~nyx.config.Settings` and
applies ``enabled_adapters`` as the deployment filter. Callers decide which settings they have.
"""

from __future__ import annotations

from collections.abc import Callable

from nyx.adapters.agent_transcript.config import AgentTranscriptConfig
from nyx.adapters.agent_transcript.hermes import HermesAdapter
from nyx.adapters.base import (
    Adapter,
    AdapterHealth,
    AdapterInput,
    AdapterRegistry,
    AdapterState,
    UnknownAdapterError,
)
from nyx.adapters.git_ci.adapter import GitCiAdapter
from nyx.adapters.git_ci.config import GitCiConfig
from nyx.config import Settings
from nyx.domain.base import utc_now


def build_adapter_registry(settings: Settings | None = None) -> AdapterRegistry:
    """Return the adapter registry for this deployment.

    Every registered adapter is constructed, then filtered by ``enabled_adapters``. A disabled
    adapter is registered but not enabled, which is what lets `nyx doctor` report
    "registered but disabled" honestly (ADR 0008) and lets the watcher skip it without
    constructing a client it will never call.

    A misconfigured **disabled** adapter still appears in the inventory as a descriptor, but is
    not registered as a working adapter: `nyx doctor` must be able to say "git-ci is installed but
    its repository is not set" rather than a bare "none enabled", which is what an operator needs
    to find the misconfiguration. What it must not do is let that misconfiguration break the
    command -- so the descriptor is present and the adapter is absent.

    An **enabled** adapter that cannot be constructed raises :class:`~pydantic.ValidationError`:
    that is a misconfiguration the caller must fix, and the message names the field.
    """

    effective = settings or Settings()
    enabled = set(effective.enabled_adapters)
    registry = AdapterRegistry(is_enabled=lambda adapter_id: adapter_id in enabled)

    _register(
        registry,
        GitCiAdapter,
        lambda: GitCiAdapter(GitCiConfig.from_settings(effective)),
        adapter_id="git-ci",
        enabled="git-ci" in enabled,
    )
    # Each agent-transcript adapter shares one config shape but its own auto-detect store, so the
    # config is built per adapter from that adapter's default path. Adding an agent is one import
    # and one register() call here -- the shared base does the rest (ADR 0010).
    _register(
        registry,
        HermesAdapter,
        lambda: HermesAdapter(
            AgentTranscriptConfig.from_settings(
                effective, default_db=HermesAdapter.default_db_path()
            )
        ),
        adapter_id="hermes",
        enabled="hermes" in enabled,
    )
    return registry


def _register(
    registry: AdapterRegistry,
    adapter_type: type[Adapter],
    factory: Callable[[], Adapter],
    *,
    adapter_id: str,
    enabled: bool,
) -> None:
    """Register one adapter, or its descriptor alone when it is disabled and misconfigured.

    ``enabled`` decides whether a construction failure is the caller's problem. Raising for a
    disabled adapter would make an unconfigured-but-off adapter break every caller -- including
    ``nyx doctor`` on the most common configuration there is -- so the descriptor is registered
    instead and the adapter is not.
    """

    try:
        registry.register(factory())
        return
    except Exception as construction_error:  # noqa: BLE001 - a disabled adapter's misconfig
        if enabled:
            raise
        reason = _missing_field(construction_error)
    registry.register(_UnconstructibleAdapter(adapter_type, adapter_id=adapter_id, reason=reason))


def _missing_field(exc: Exception) -> str:
    """The setting an adapter's construction failure names, or a generic fallback.

    ``pydantic.ValidationError`` carries the offending field in ``loc``, which is the difference
    between "its required configuration is not set" and "repository is not set" -- and only the
    second tells an operator what to type.
    """

    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors(include_input=False)[0]
        except Exception:  # noqa: BLE001 - a diagnostic degrades, it does not raise
            first = {}
        field = first.get("loc", ("configuration",))[0]
        if isinstance(field, str) and field:
            return f"{field} is not set"
    return "its required configuration is not set"


class _UnconstructibleAdapter:
    """Stand-in for an adapter that is installed but cannot be constructed in this deployment.

    It satisfies the :class:`~nyx.adapters.base.Adapter` protocol's *read-only* attributes so the
    registry can describe it, and refuses the two methods that would need a real client: probing an
    adapter that does not exist is not a probe. The id and version are the adapter's own, so the
    inventory names the right thing.
    """

    def __init__(
        self, adapter_type: type[Adapter], *, adapter_id: str, reason: str = "unconfigured"
    ) -> None:
        self.adapter_id = adapter_id
        self.version = adapter_type.version
        self._reason = reason
        self.description = (
            f"{adapter_id} is installed but not constructible in this deployment; {reason}"
        )
        self.permissions = list(adapter_type.permissions)
        self.can_observe = list(adapter_type.can_observe)
        self.cannot_observe = list(adapter_type.cannot_observe)

    def health(self) -> AdapterHealth:
        """An adapter that cannot be constructed is ``DEGRADED``, and says why."""

        return AdapterHealth(
            adapter_id=self.adapter_id,
            version=self.version,
            state=AdapterState.DEGRADED,
            detail=f"{self.adapter_id} is registered but not constructible: {self._reason}",
            checked_at=utc_now(),
        )

    def normalize(self, raw: object) -> list[AdapterInput]:
        """Never reached: an unconstructible adapter is not enabled, so nothing calls this."""

        raise UnknownAdapterError(
            f"{self.adapter_id} is registered but not constructible in this deployment"
        )
