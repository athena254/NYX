"""The adapter boundary: protocol, descriptor, health, and registry (SPEC §14, ADR 0008).

An adapter is a **producer**. It observes an external system and hands NYX the same
producer input models a human would submit by hand — ``EventInput``, ``ClaimInput``,
``EvidenceInput``. It is never a new ingestion path: its output goes through the ordinary
gateway, so it inherits redaction, schema validation, identity deduplication, and audit for
free (``src/nyx/adapters/base.py`` → ``nyx.ingestion.inputs`` → ``IngestionGateway``).

Two rules this module exists to make enforceable:

* **Adapters import up, never across.** A module under ``src/nyx/adapters/`` may import
  ``nyx.domain`` and ``nyx.ingestion`` and nothing from the deterministic core. Gate G7
  is the executable form of that rule.
* **Core never imports adapters.** ``api/app.py`` and ``cli/main.py`` construct adapters;
  nothing under ``critics``, ``findings``, ``disputes``, ``policies``, ``storage``,
  ``retention``, ``auth``, ``systems``, or ``audit`` may import ``nyx.adapters``.

The registry deliberately holds the **code inventory** (what adapters exist) while
``Settings.enabled_adapters`` holds the **deployment filter** (which are switched on).
Keeping them apart is what lets ``nyx doctor`` report "registered but disabled" honestly —
Phase 4 defect #7 was a doctor check that reported *configuration* as *availability*.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Protocol, runtime_checkable

from pydantic import Field, StringConstraints

from nyx.domain.base import Identifier, NyxModel, UtcDatetime, utc_now
from nyx.ingestion.inputs import ClaimInput, EventInput, EvidenceInput

#: The input models an adapter may emit. Returning domain models instead would skip
#: redaction and schema validation, which is the one thing the boundary guarantees.
AdapterInput = EventInput | ClaimInput | EvidenceInput


class AdapterState(StrEnum):
    """What a health probe established. ``DISABLED`` is set by configuration, not by a probe."""

    UNKNOWN = "unknown"
    OK = "ok"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class AdapterHealth(NyxModel):
    """The outcome of probing one adapter.

    ``detail`` is human-readable and must never carry a credential: an adapter that
    reports its own failure is the most likely place a URL with a token would appear.
    """

    adapter_id: Identifier
    version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    state: AdapterState
    detail: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    checked_at: UtcDatetime


class AdapterDescriptor(NyxModel):
    """The public description of one registered adapter.

    ``permissions`` records what the adapter is *allowed* to touch, and the
    ``can_observe`` / ``cannot_observe`` pair states what it can and cannot see, so a
    caller can interpret an empty result: "the adapter saw nothing" and "the adapter
    cannot see that" are different claims.
    """

    adapter_id: Identifier
    version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    permissions: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=64
    )
    can_observe: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=32
    )
    cannot_observe: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=32
    )


@runtime_checkable
class Adapter(Protocol):
    """The structural contract every adapter implements.

    A ``Protocol`` and not a base class: nothing in ``domain`` knows adapters exist, and
    an adapter that satisfies these attributes and methods type-checks where it is
    registered without inheriting anything.
    """

    adapter_id: str
    version: str
    description: str
    permissions: list[str]
    can_observe: list[str]
    cannot_observe: list[str]

    def health(self) -> AdapterHealth:
        """Probe the adapter. Never raises: a failure is reported as ``DEGRADED``."""
        ...

    def normalize(self, raw: object) -> list[AdapterInput]:
        """Turn one external observation into zero or more producer inputs."""
        ...


class UnknownAdapterError(Exception):
    """Raised when a caller asks the registry for an adapter that is not registered."""


class AdapterRegistry:
    """The code inventory of adapters, plus the deployment filter over it.

    ``is_enabled`` is injected (``Settings.enabled_adapters`` is the production source) so
    the registry stays free of configuration. ``states()`` probes **only enabled**
    adapters: a disabled adapter is not running, so there is nothing to probe, and
    probing a disabled one would mean constructing it just to hang a health check on it.
    """

    def __init__(
        self,
        *,
        is_enabled: Callable[[str], bool] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._adapters: dict[str, Adapter] = {}
        self._is_enabled = is_enabled if is_enabled is not None else (lambda adapter_id: True)
        self._clock = clock

    def register(self, adapter: Adapter) -> None:
        """Register an adapter. Re-registering the same id replaces the previous entry."""

        self._adapters[adapter.adapter_id] = adapter

    def get(self, adapter_id: str) -> Adapter:
        """Return the adapter registered under ``adapter_id``.

        Raises :class:`UnknownAdapterError` — an unknown adapter is a programming error,
        not a caller-supplied value, so it is not modelled as a domain error code.
        """

        try:
            return self._adapters[adapter_id]
        except KeyError:
            raise UnknownAdapterError(
                f"unknown adapter {adapter_id!r}; registered adapters: {sorted(self._adapters)}"
            ) from None

    def registered(self) -> list[AdapterDescriptor]:
        """Every registered adapter, in stable id order. This is the code inventory."""

        return [
            self._describe(adapter)
            for adapter in sorted(self._adapters.values(), key=lambda item: item.adapter_id)
        ]

    def enabled(self) -> list[AdapterDescriptor]:
        """The registered adapters the deployment filter switches on."""

        return [
            descriptor
            for descriptor in self.registered()
            if self._is_enabled(descriptor.adapter_id)
        ]

    def states(self) -> list[AdapterHealth]:
        """Probe every **enabled** adapter, in stable id order. Never raises.

        An adapter whose ``health()`` raises is reported as ``DEGRADED`` with the
        exception's *type name* only — never its message, which can echo a URL carrying a
        credential. One broken adapter must not take the whole registry down.
        """

        states: list[AdapterHealth] = []
        for adapter in sorted(self._adapters.values(), key=lambda item: item.adapter_id):
            if not self._is_enabled(adapter.adapter_id):
                continue
            try:
                states.append(adapter.health())
            except Exception as exc:  # noqa: BLE001 - record the failure, do not raise
                states.append(
                    AdapterHealth(
                        adapter_id=adapter.adapter_id,
                        version=adapter.version,
                        state=AdapterState.DEGRADED,
                        detail=(
                            f"{adapter.adapter_id} health check raised "
                            f"{type(exc).__name__}; no message is included because an "
                            "exception message can echo a request URL"
                        ),
                        checked_at=self._clock(),
                    )
                )
        return states

    @staticmethod
    def _describe(adapter: Adapter) -> AdapterDescriptor:
        return AdapterDescriptor(
            adapter_id=adapter.adapter_id,
            version=adapter.version,
            description=adapter.description,
            permissions=list(adapter.permissions),
            can_observe=list(adapter.can_observe),
            cannot_observe=list(adapter.cannot_observe),
        )


__all__ = [
    "Adapter",
    "AdapterDescriptor",
    "AdapterHealth",
    "AdapterInput",
    "AdapterRegistry",
    "AdapterState",
    "UnknownAdapterError",
]
