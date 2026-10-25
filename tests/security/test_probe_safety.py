"""The probe registry's safety boundary: read-only default, budgets, kill switch, mutation gate.

These are security tests. They are written from the threat, not from the implementation: the
question each one answers is "what happens when a probe tries to do the thing it must not", and
the answer must be "it is refused", never "it is logged and then run".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nyx.domain.authorisation import Authorisation
from nyx.domain.base import PrincipalRef, utc_now
from nyx.domain.enums import Environment, PrincipalKind, ProbeKind, RiskAction
from nyx.evidence.probes import (
    ProbeContext,
    ReexecutionProbe,
    StateCheckProbe,
    builtin_probes,
)
from nyx.evidence.registry import Budget, ProbeRegistry, ProbeRegistryError

_ACTOR = PrincipalRef(id="agent:claude", kind=PrincipalKind.AGENT)
_APPROVER = PrincipalRef(id="human:operator", kind=PrincipalKind.HUMAN)


#: ProbeContext has no keyword for a probe's own arguments -- they live in ``extra``.
_CONTEXT_FIELDS = frozenset(
    {
        "subject",
        "subject_version",
        "environment",
        "collector",
        "claim_id",
        "timeout_seconds",
        "extra",
    }
)


def _context(**overrides: object) -> ProbeContext:
    fields: dict[str, object] = {
        "subject": "booking-service",
        "subject_version": "abc123",
        "environment": Environment.STAGING,
        "claim_id": "clm_1",
    }
    extra: dict[str, object] = {}
    for key, value in overrides.items():
        if key in _CONTEXT_FIELDS:
            fields[key] = value
        else:
            extra[key] = value
    return ProbeContext(extra=extra, **fields)  # type: ignore[arg-type]


def _registry(**kwargs: object) -> ProbeRegistry:
    registry = ProbeRegistry(**kwargs)  # type: ignore[arg-type]
    registry.register_all(builtin_probes())
    return registry


def _authorisation(
    *,
    scope_ref: str = "booking-service",
    expires_at: datetime | None = None,
) -> Authorisation:
    return Authorisation(
        action="probe.state-check",
        risk_action=RiskAction.OVERWRITE_PRODUCTION_STATE,
        actor=_ACTOR,
        approver=_APPROVER,
        environment=Environment.STAGING,
        scope_ref=scope_ref,
        approved_at=utc_now(),
        expires_at=expires_at,
    )


# --- the read-only default -------------------------------------------------


def test_a_read_only_probe_runs() -> None:
    registry = _registry()
    observation = registry.run_probe(
        "probe:state-check",
        "examples/claim.json",
        _context(path="examples/claim.json"),
    )
    assert observation.result.value == "observed"


def test_the_registry_refuses_an_unknown_probe() -> None:
    with pytest.raises(ProbeRegistryError, match="unknown probe"):
        _registry().run_probe("probe:does-not-exist", "x", _context())


def test_the_registry_lists_probes_with_their_read_only_flag() -> None:
    infos = _registry().list_probes()
    assert [info.probe_id for info in infos] == sorted(info.probe_id for info in infos)
    assert all(info.read_only is True for info in infos)
    assert {info.kind for info in infos} == {kind.value for kind in ProbeKind}


# --- the kill switch -------------------------------------------------------


def test_the_kill_switch_stops_read_only_probes_too() -> None:
    """A capability that cannot be stopped as a whole is not switchable."""

    registry = _registry(enabled=False)
    with pytest.raises(ProbeRegistryError, match="disabled"):
        registry.run_probe("probe:state-check", "examples/claim.json", _context())


def test_the_kill_switch_also_blocks_lookup() -> None:
    registry = _registry(enabled=False)
    with pytest.raises(ProbeRegistryError, match="disabled"):
        registry.get("probe:state-check")


def test_the_kill_switch_does_not_stop_listing() -> None:
    """Listing is not a probe run, so the switch does not hide what is installed."""

    registry = _registry(enabled=False)
    assert len(registry.list_probes()) == 5


# --- the mutating-probe gate ----------------------------------------------


class _MutatingProbe:
    """A probe that declares it mutates state. Deliberately minimal."""

    probe_id = "probe:mutating"
    probe_version = "1.0.0"
    kind = ProbeKind.STATE_CHECK
    read_only = False
    description = "Test double that declares read_only=False."

    def run(self, target: str, context: ProbeContext) -> object:
        from nyx.domain.enums import ProbeOutcome, VerificationStrength
        from nyx.domain.observation import Observation

        return Observation(
            probe_id=self.probe_id,
            probe_version=self.probe_version,
            subject=context.subject,
            collected_at=utc_now(),
            collector=context.collector or PrincipalRef(id="nyx:probe", kind=PrincipalKind.SYSTEM),
            method=self.kind,
            result=ProbeOutcome.OBSERVED,
            environment=context.environment,
            read_only=self.read_only,
            verification_strength=VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE,
        )


def test_a_mutating_probe_is_refused_while_the_gate_is_closed() -> None:
    registry = _registry()
    registry.register(_MutatingProbe())
    with pytest.raises(ProbeRegistryError, match="gate is closed"):
        registry.run_probe("probe:mutating", "x", _context())


def test_a_mutating_probe_is_refused_without_an_authorisation_lookup() -> None:
    """No lookup means no authorisation can be checked, so the run is refused."""

    registry = _registry(allow_mutating=True)
    registry.register(_MutatingProbe())
    with pytest.raises(ProbeRegistryError, match="no authorisation lookup"):
        registry.run_probe("probe:mutating", "x", _context())


def test_a_mutating_probe_is_refused_when_no_authorisation_covers_the_action() -> None:
    registry = _registry(allow_mutating=True)
    registry.register(_MutatingProbe())
    registry.set_authorisation_lookup(lambda **_: None)
    with pytest.raises(ProbeRegistryError, match="no authorisation covers"):
        registry.run_probe(
            "probe:mutating",
            "x",
            _context(actor_id=_ACTOR.id, action="probe.mutating", action_scope="booking-service"),
        )


def test_a_mutating_probe_is_refused_when_the_authorisation_has_expired() -> None:
    registry = _registry(allow_mutating=True)
    registry.register(_MutatingProbe())
    expired = _authorisation(expires_at=datetime.now(UTC) - timedelta(hours=1))
    registry.set_authorisation_lookup(lambda **_: expired)
    with pytest.raises(ProbeRegistryError, match="expired"):
        registry.run_probe(
            "probe:mutating",
            "x",
            _context(actor_id=_ACTOR.id, action="probe.mutating", action_scope="booking-service"),
        )


def test_a_mutating_probe_is_refused_when_the_scope_does_not_cover_the_action() -> None:
    registry = _registry(allow_mutating=True)
    registry.register(_MutatingProbe())
    registry.set_authorisation_lookup(lambda **_: _authorisation(scope_ref="payments-service"))
    with pytest.raises(ProbeRegistryError, match="does not cover"):
        registry.run_probe(
            "probe:mutating",
            "x",
            _context(actor_id=_ACTOR.id, action="probe.mutating", action_scope="booking-service"),
        )


def test_a_mutating_probe_runs_when_authorised_unexpired_and_in_scope() -> None:
    registry = _registry(allow_mutating=True)
    registry.register(_MutatingProbe())
    registry.set_authorisation_lookup(lambda **_: _authorisation())
    observation = registry.run_probe(
        "probe:mutating",
        "x",
        _context(actor_id=_ACTOR.id, action="probe.mutating", action_scope="booking-service"),
    )
    assert observation.read_only is False


def test_the_gate_defaults_to_closed_in_settings() -> None:
    """The configuration default is the safe one, not the convenient one."""

    from nyx.config import Settings

    assert Settings().probes_allow_mutating is False
    assert Settings().probes_enabled is False


def test_settings_refuse_mutating_probes_with_the_kill_switch_closed() -> None:
    from nyx.config import Settings

    with pytest.raises(ValueError, match="NYX_PROBES_ALLOW_MUTATING"):
        Settings(probes_allow_mutating=True, probes_enabled=False)


# --- budgets --------------------------------------------------------------


def test_a_batch_over_its_step_budget_is_refused() -> None:
    registry = _registry()
    probes = [("probe:state-check", "examples/claim.json", _context()) for _ in range(4)]
    with pytest.raises(ProbeRegistryError, match="step budget"):
        registry.run_many(probes, budget=Budget(max_steps=2, timeout_seconds=60))


def test_a_batch_past_its_deadline_is_refused() -> None:
    """The deadline is shared across the batch, which is what bounds the whole run."""

    started = datetime(2026, 10, 25, tzinfo=UTC)
    late = started + timedelta(seconds=30)
    # The first tick starts the budget; every later tick is past it.
    ticks = iter([started])

    def clock() -> datetime:
        try:
            return next(ticks)
        except StopIteration:
            return late

    registry = ProbeRegistry(clock=clock)
    registry.register(StateCheckProbe())
    with pytest.raises(ProbeRegistryError, match="deadline"):
        registry.run_many(
            [
                ("probe:state-check", "examples/claim.json", _context()),
                ("probe:state-check", "examples/event.json", _context()),
            ],
            budget=Budget(max_steps=10, timeout_seconds=5),
        )


def test_a_batch_within_budget_completes() -> None:
    registry = _registry()
    observations = registry.run_many(
        [
            ("probe:state-check", "examples/claim.json", _context()),
            ("probe:state-check", "examples/event.json", _context()),
        ],
        budget=Budget(max_steps=5, timeout_seconds=60),
    )
    assert len(observations) == 2


def test_the_registry_records_a_read_only_observation_for_the_run() -> None:
    """The observation produced by a registry run is the same record a direct call produces."""

    registry = _registry()
    observation = registry.run_probe(
        "probe:reexecution",
        "tests",
        _context(runner=lambda: (0, "ok", "")),
    )
    assert observation.read_only is True
    assert observation.probe_id == "probe:reexecution"


def test_an_unknown_probe_is_not_silently_skipped() -> None:
    """A typo in a probe id must fail the run, not produce an empty observation."""

    with pytest.raises(ProbeRegistryError):
        _registry().run_probe("probe:state_chec", "x", _context())


def test_the_reexecution_probe_through_the_registry_is_read_only() -> None:
    """Every built-in probe reachable through the registry declares read-only."""

    registry = _registry()
    for info in registry.list_probes():
        assert info.read_only is True, info.probe_id
    assert isinstance(ReexecutionProbe(), object)
