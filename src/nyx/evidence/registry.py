"""Probe registration, budgets, and the read-only default.

A probe is registered explicitly — there is no discovery — for the same reason critics are: the
set of things NYX can do to another system must be visible in one file, because an undiscoverable
probe is an unauditable probe.

Three safeguards live here, and each exists because the alternative is a false sense of safety:

* **Read-only by default.** ``run_probe`` refuses a probe that declares ``read_only=False`` unless
  the mutating gate is open *and* an authorisation record covers the action.
* **Budgets.** A step budget and a wall-clock deadline bound one run, and the deadline is checked
  between steps rather than by wrapping the probe in a thread, which would leave the probe's own
  work unfinished and the observation missing.
* **Kill switch.** ``enabled=False`` makes the registry refuse every probe, including read-only
  ones. A probe that can be individually disabled but not globally stopped is not switchable.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from nyx.domain.authorisation import Authorisation
from nyx.domain.base import utc_now
from nyx.domain.enums import ProbeKind, ProbeOutcome, RiskAction
from nyx.domain.observation import Observation
from nyx.evidence.isolated import (
    IsolatedExecutionRefused,
    IsolatedPayloadRefused,
    execute_isolated_probe,
    prepare_trusted_probe,
)
from nyx.evidence.probes import Probe, ProbeContext, Target, make_observation


class ProbeRegistryError(Exception):
    """Raised when a probe is unknown, disabled, over budget, or not authorised to mutate."""


class ProbeExecutionRefused(ProbeRegistryError):
    """The trusted extension cannot be run in cancellable process isolation."""


@dataclass(frozen=True)
class ProbeInfo:
    """The public description of one registered probe."""

    probe_id: str
    probe_version: str
    kind: str
    read_only: bool
    description: str


@dataclass(frozen=True)
class Budget:
    """What one probe run may consume.

    ``max_steps`` counts distinct probe invocations, not internal operations: a probe that reads
    ten rows is one step. ``deadline`` is a wall-clock bound checked between steps.
    """

    max_steps: int = 10
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class _FrozenProbe:
    """Configuration-time metadata snapshot; runtime never reads extension attributes."""

    probe_id: str
    probe_version: str
    kind: ProbeKind
    read_only: bool
    description: str

    def run(self, target: Target, context: ProbeContext) -> Observation:
        raise RuntimeError("frozen probe descriptors are registry metadata, not executable probes")


@dataclass(frozen=True)
class _RegisteredProbe:
    descriptor: _FrozenProbe
    prepared_payload: bytes


class ProbeRegistry:
    """The explicit set of probes NYX may run, with its safeguards."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        allow_mutating: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._probes: dict[str, _RegisteredProbe] = {}
        self._enabled = enabled
        self._allow_mutating = allow_mutating
        self._clock = clock
        self._authorisation_lookup: Callable[..., Authorisation | None] | None = None

    # --- registration ------------------------------------------------------

    def register(self, probe: Probe) -> None:
        """Prepare trusted code; re-registering refreshes immutable configuration."""

        descriptor = _freeze_probe(probe)
        self._probes[descriptor.probe_id] = _RegisteredProbe(
            descriptor=descriptor,
            prepared_payload=prepare_trusted_probe(probe),
        )

    def register_all(self, probes: Iterable[Probe]) -> None:
        for probe in probes:
            self.register(probe)

    def list_probes(self) -> list[ProbeInfo]:
        """Return the registered probes in stable id order."""

        return [
            ProbeInfo(
                probe_id=registered.descriptor.probe_id,
                probe_version=registered.descriptor.probe_version,
                kind=registered.descriptor.kind.value,
                read_only=registered.descriptor.read_only,
                description=registered.descriptor.description,
            )
            for registered in sorted(
                self._probes.values(), key=lambda item: item.descriptor.probe_id
            )
        ]

    def get(self, probe_id: str) -> Probe:
        """Return the probe registered under ``probe_id``."""

        self._require_enabled()
        try:
            return cast(Probe, self._probes[probe_id].descriptor)
        except KeyError:
            raise ProbeRegistryError(
                f"unknown probe {probe_id!r}; registered probes: {sorted(self._probes)}"
            ) from None

    # --- safety wiring -----------------------------------------------------

    def set_authorisation_lookup(self, lookup: Callable[..., Authorisation | None] | None) -> None:
        """Inject the authorisation read path used by the mutating-probe gate.

        Injected rather than imported so this module stays free of storage imports and can be
        exercised without a database.
        """

        self._authorisation_lookup = lookup

    # --- execution ---------------------------------------------------------

    def run_probe(
        self,
        probe_id: str,
        target: Target,
        context: ProbeContext,
        *,
        budget: Budget | None = None,
    ) -> Observation:
        """Run one probe under its budget, or raise.

        Raises :class:`ProbeRegistryError` rather than returning a synthetic observation when the
        run is refused (disabled, unknown, unauthorised), because a refusal is an operational
        fact a caller must handle, not a finding about the target. A probe that runs and finds
        nothing returns ``NOT_OBSERVED`` as normal.
        """

        deadline_monotonic = time.monotonic() + (budget or Budget()).timeout_seconds
        registered = self._registered(probe_id)
        self._guard_mutating(registered.descriptor, context)
        return self._run_with_budget(
            registered,
            target,
            context,
            budget or Budget(),
            deadline_monotonic=deadline_monotonic,
        )

    def run_many(
        self,
        probes: Sequence[tuple[str, Target, ProbeContext]],
        *,
        budget: Budget | None = None,
    ) -> list[Observation]:
        """Run several probes under one shared budget.

        The deadline is shared across the whole batch, which is what makes a batch bounded: one
        slow probe consumes the budget the later ones would have used.
        """

        resolved = budget or Budget()
        deadline = self._clock().timestamp() + resolved.timeout_seconds
        deadline_monotonic = time.monotonic() + resolved.timeout_seconds
        observations: list[Observation] = []
        for index, (probe_id, target, context) in enumerate(probes):
            if index >= resolved.max_steps:
                raise ProbeRegistryError(
                    f"probe batch exceeded its step budget of {resolved.max_steps}"
                )
            if self._clock().timestamp() > deadline or time.monotonic() > deadline_monotonic:
                raise ProbeRegistryError(
                    f"probe batch exceeded its deadline of {resolved.timeout_seconds}s"
                )
            registered = self._registered(probe_id)
            self._guard_mutating(registered.descriptor, context)
            observations.append(
                self._run_with_budget(
                    registered,
                    target,
                    context,
                    resolved,
                    deadline_monotonic=deadline_monotonic,
                )
            )
        return observations

    # --- internals ---------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise ProbeRegistryError(
                "the probe registry is disabled; enable it explicitly before running any probe"
            )

    def _registered(self, probe_id: str) -> _RegisteredProbe:
        self._require_enabled()
        try:
            return self._probes[probe_id]
        except KeyError:
            raise ProbeRegistryError(
                f"unknown probe {probe_id!r}; registered probes: {sorted(self._probes)}"
            ) from None

    def _guard_mutating(self, probe: _FrozenProbe, context: ProbeContext) -> None:
        if probe.read_only:
            return
        if type(context.subject) is not str or type(context.extra) is not dict:
            raise ProbeExecutionRefused(
                "mutating probe runtime context must use exact str subject and "
                "exact JSON dict extra"
            )
        if not self._allow_mutating:
            raise ProbeRegistryError(
                f"probe {probe.probe_id!r} declares read_only=False and the mutating-probe gate "
                "is closed; a probe that mutates state may only run behind an authorisation "
                "record, and the gate is off"
            )
        if self._authorisation_lookup is None:
            raise ProbeRegistryError(
                f"probe {probe.probe_id!r} declares read_only=False but no authorisation lookup "
                "is wired, so no authorisation can be checked"
            )
        action_scope = context.extra.get("action_scope", context.subject)
        actor_id = context.extra.get("actor_id", "")
        action = context.extra.get("action", probe.probe_id)
        if type(action_scope) is not str or type(actor_id) is not str or type(action) is not str:
            raise ProbeExecutionRefused(
                "mutating probe authorisation fields must be exact strings in runtime JSON data"
            )
        now = self._clock()
        found = self._authorisation_lookup(
            actor_id=actor_id,
            action=action,
            risk_action=RiskAction.OVERWRITE_PRODUCTION_STATE,
            action_scope=action_scope,
            at=now,
        )
        if found is None:
            raise ProbeRegistryError(
                f"probe {probe.probe_id!r} declares read_only=False and no authorisation covers "
                f"scope {action_scope!r}; refusing to run"
            )
        # The lookup finds a candidate; the gate decides whether it is valid. Delegating the
        # decision would let a lookup that returns "the most recent one" authorise a run that an
        # expired or out-of-scope record must not authorise.
        if found.is_expired(now):
            raise ProbeRegistryError(
                f"probe {probe.probe_id!r} declares read_only=False and the covering "
                f"authorisation {found.id} expired at {found.expires_at}; refusing to run"
            )
        if not found.scope_matches(action_scope):
            raise ProbeRegistryError(
                f"probe {probe.probe_id!r} declares read_only=False and the covering "
                f"authorisation {found.id} is scoped to {found.scope_ref!r}, which does not "
                f"cover {action_scope!r}; refusing to run"
            )

    def _run_with_budget(
        self,
        registered: _RegisteredProbe,
        target: Target,
        context: ProbeContext,
        budget: Budget,
        *,
        deadline_monotonic: float,
    ) -> Observation:
        if time.monotonic() >= deadline_monotonic:
            raise ProbeRegistryError(
                f"probe {registered.descriptor.probe_id!r} was already past its "
                f"{budget.timeout_seconds}s deadline"
            )
        try:
            result = execute_isolated_probe(
                registered.prepared_payload,
                target,
                context,
                deadline_monotonic=deadline_monotonic,
            )
        except IsolatedPayloadRefused as exc:
            raise ProbeExecutionRefused(str(exc)) from None
        except IsolatedExecutionRefused as exc:
            raise ProbeExecutionRefused(str(exc)) from None
        if result.status == "completed" and isinstance(result.value, Observation):
            return result.value
        failure = "invalid_output" if result.status == "completed" else result.status
        return make_observation(
            probe=cast(Probe, registered.descriptor),
            target=target,
            context=context,
            outcome=ProbeOutcome.ERROR,
            detail=(
                f"Probe {registered.descriptor.probe_id!r} did not complete inside its isolated "
                "execution boundary "
                f"({failure})."
            ),
        )


def _freeze_probe(probe: Probe) -> _FrozenProbe:
    """Read trusted extension metadata only during explicit registration preparation."""

    probe_id = probe.probe_id
    probe_version = probe.probe_version
    kind = probe.kind
    read_only = probe.read_only
    description = probe.description
    if (
        type(probe_id) is not str
        or type(probe_version) is not str
        or type(kind) is not ProbeKind
        or type(read_only) is not bool
        or type(description) is not str
    ):
        raise ProbeExecutionRefused(
            "trusted probe metadata is not a supported immutable configuration"
        )
    return _FrozenProbe(probe_id, probe_version, kind, read_only, description)
