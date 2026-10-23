"""Unit tests for the watcher's cycle (ADR 0009).

These drive :class:`nyx.watch.runner.WatchCycle` against **fake adapters and a real migrated
database** -- the adapter boundary is the thing under test here, so a fake adapter is the right
seam. Nothing in this file touches the network or the filesystem.

Four properties are the point, and each is asserted by *driving* the code rather than by
checking a counter the implementation also computes:

* **A cycle never raises.** Every adapter misbehaviour modelled here -- a probe that raises, a
  probe that reports ``DEGRADED``, a fetch that raises -- produces a ``CycleOutcome``, never an
  exception.
* **An error is not an empty observation.** A fetch that raises is recorded as ``DEGRADED`` with
  the failure in ``detail``; it is never reported as "fetched nothing".
* **One failure does not stop the next adapter.** Two adapters, the first broken: the second
  still fetches, ingests, and is audited.
* **The watcher cannot judge.** No disposition, rebuttal, or lifecycle write is reachable from
  this module, which the AST test below makes executable.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import func, select

from nyx.adapters.base import (
    Adapter,
    AdapterHealth,
    AdapterInput,
    AdapterRegistry,
    AdapterState,
)
from nyx.config import Settings
from nyx.storage.db import SessionFactory
from nyx.storage.tables import DispositionRow, RebuttalRow
from nyx.watch.runner import (
    CYCLE_ACTION,
    WATCHER_PRINCIPAL,
    CycleOutcome,
    MissingAdapterInventoryError,
    WatchCycle,
)

_RUNNER_SOURCE = Path(__file__).resolve().parents[2] / "src" / "nyx" / "watch" / "runner.py"

#: Modules the watcher must never import. ADR 0009 is explicit that the watcher cannot judge, so
#: reaching for the dispute or lifecycle surface is the one mistake this file exists to catch.
FORBIDDEN_MODULES = (
    "nyx.disputes",
    "nyx.findings.lifecycle",
    "nyx.policies",
    "nyx.retention",
)


# ----------------------------------------------------------------- fake adapters


class _Clock:
    """A clock that advances by a fixed step on every read, so a cycle's timestamps are exact."""

    def __init__(self, start: datetime, step: timedelta = timedelta(seconds=1)) -> None:
        self._value = start
        self._step = step

    def __call__(self) -> datetime:
        current = self._value
        self._value = current + self._step
        return current


def _health(adapter_id: str, *, state: AdapterState, detail: str) -> AdapterHealth:
    return AdapterHealth(
        adapter_id=adapter_id,
        version="1.0.0",
        state=state,
        detail=detail,
        checked_at=datetime.now(UTC),
    )


class _FakeAdapter:
    """A structural ``Adapter`` whose behaviour is configured per test.

    ``inputs`` is what ``fetch_for_ref`` returns; ``fetch_error`` is what it raises instead.
    Neither is a domain model: the inputs are the producer *input* models the gateway validates.
    """

    def __init__(
        self,
        adapter_id: str,
        *,
        inputs: list[AdapterInput] | None = None,
        state: AdapterState = AdapterState.OK,
        health_detail: str = "the fake adapter is available",
        fetch_error: Exception | None = None,
        probe_error: Exception | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.version = "1.0.0"
        self.description = f"a fake adapter named {adapter_id}"
        self.permissions = ["test.read"]
        self.can_observe = ["synthetic producer inputs"]
        self.cannot_observe = ["anything a real adapter would observe"]
        self._inputs = inputs if inputs is not None else []
        self._state = state
        self._health_detail = health_detail
        self._fetch_error = fetch_error
        self._probe_error = probe_error
        self.probes = 0
        self.fetches = 0

    def health(self) -> AdapterHealth:
        self.probes += 1
        if self._probe_error is not None:
            raise self._probe_error
        return _health(self.adapter_id, state=self._state, detail=self._health_detail)

    def normalize(self, raw: object) -> list[AdapterInput]:
        return list(self._inputs)

    def fetch_for_ref(self, ref: str) -> list[AdapterInput]:
        self.fetches += 1
        if self._fetch_error is not None:
            raise self._fetch_error
        return list(self._inputs)


class _BrokenError(Exception):
    """An adapter failure that is *not* a ``GitCiError``, so its message is never quoted."""


# ----------------------------------------------------------------- helpers


def _settings(tmp_path: Path, database: Path, *, adapters: list[str] | None = None) -> Settings:
    return Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        data_dir=tmp_path,
        enabled_adapters=adapters if adapters is not None else ["fake"],
        watch_enabled=True,
        watch_ref="main",
    )


def _event_input(producer_event_id: str) -> AdapterInput:
    """One minimal, valid ``EventInput`` -- the shape a real adapter emits."""

    from nyx.domain.enums import RetentionClass, TrustClass
    from nyx.ingestion.inputs import EventInput

    return EventInput(
        source_system_id="sys_watch_unit",
        event_type="ci.check.completed",
        occurred_at=datetime(2026, 10, 22, 9, 0, 0, tzinfo=UTC),
        correlation_id=producer_event_id,
        payload={"commit": "watch-unit-01", "name": "unit-check"},
        trust_class=TrustClass.OBSERVED,
        labels=[],
        retention_class=RetentionClass.STANDARD,
        producer_event_id=producer_event_id,
    )


def _registry(*adapters: _FakeAdapter) -> AdapterRegistry:
    registry = AdapterRegistry(is_enabled=lambda adapter_id: True)
    for adapter in adapters:
        registry.register(adapter)
    return registry


def _cycle(
    session_factory: SessionFactory,
    settings: Settings,
    inventory: AdapterRegistry,
    *,
    clock: _Clock | None = None,
) -> WatchCycle:
    """Build a cycle whose adapter inventory is the given registry.

    ``AdapterRegistry.enabled()`` returns descriptors rather than adapters, so the registry is
    wrapped in the narrow ``AdapterInventory`` view the cycle needs. That wrapper lives here,
    not in the runner, because the runner never imports ``nyx.adapters`` (gate G7).
    """

    class _Inventory:
        def enabled(self) -> list[Any]:
            return [
                cast(Any, inventory.get(descriptor.adapter_id))
                for descriptor in inventory.enabled()
            ]

    return WatchCycle(
        session_factory=session_factory,
        settings=settings,
        inventory=_Inventory(),
        clock=clock or _Clock(datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)),
    )


def _count(session_factory: SessionFactory, model: type) -> int:
    from nyx.storage.db import session_scope

    with session_scope(session_factory) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _watch_entries(session_factory: SessionFactory) -> list[tuple[str, str, str, str]]:
    """The ``watch.cycle`` entries, as ``(action, actor_id, actor_kind, reason)``."""

    from nyx.storage.db import session_scope
    from nyx.storage.tables import AuditRow

    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(AuditRow).where(AuditRow.action == CYCLE_ACTION).order_by(AuditRow.sequence)
        )
        return [(row.action, row.actor["id"], row.actor["kind"], row.reason) for row in rows]


# ----------------------------------------------------------------- outcomes


def test_run_once_returns_one_outcome_per_enabled_adapter(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """A cycle reports every enabled adapter, in the registry's stable id order."""

    registry = _registry(
        _FakeAdapter("alpha", inputs=[_event_input("watch-alpha-1")]),
        _FakeAdapter("beta", inputs=[_event_input("watch-beta-1")]),
    )
    cycle = _cycle(session_factory, _settings(tmp_path, database), registry)

    outcomes = cycle.run_once()

    assert [outcome.adapter_id for outcome in outcomes] == ["alpha", "beta"]
    assert all(isinstance(outcome, CycleOutcome) for outcome in outcomes)


def test_a_healthy_adapter_is_reported_ok(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """A probe that reports ``OK`` is recorded as ``OK``, not as degraded.

    The second assertion is the discriminating one: with the probe stubbed to report
    ``DEGRADED`` for the same inputs, the outcome must flip. Without it, a cycle that reported
    every adapter as ``OK`` would pass this test.
    """

    healthy = _FakeAdapter("fake", inputs=[_event_input("watch-ok-1")])
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(healthy))

    outcome = cycle.run_once()[0]

    assert outcome.state == "ok"
    assert outcome.fetched == 1
    assert outcome.ingested == 1
    assert outcome.duplicates == 0
    # The adapter was probed and then fetched: the cycle did not skip either step.
    assert healthy.probes == 1
    assert healthy.fetches == 1

    # The control: identical inputs, a probe that says the adapter cannot observe anything.
    # The event id differs so the control cycle is not a duplicate of the healthy one.
    degraded = _FakeAdapter(
        "fake", inputs=[_event_input("watch-ok-2")], state=AdapterState.DEGRADED
    )
    control = _cycle(session_factory, _settings(tmp_path, database), _registry(degraded))
    other = control.run_once()[0]

    assert other.state == "degraded"
    assert other.fetched == 0
    assert degraded.probes == 1
    assert degraded.fetches == 0


def test_a_degraded_probe_is_not_fetched(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """A probe that reports ``DEGRADED`` means the adapter is not fetched at all."""

    adapter = _FakeAdapter(
        "fake", state=AdapterState.DEGRADED, health_detail="the host is unreachable"
    )
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(adapter))

    outcome = cycle.run_once()[0]

    assert outcome.state == "degraded"
    assert outcome.fetched == 0
    assert outcome.ingested == 0
    assert "the host is unreachable" in outcome.detail
    # The structural proof: a degraded adapter is never asked for records.
    assert adapter.probes == 1
    assert adapter.fetches == 0


def test_a_probe_that_raises_is_degraded_and_does_not_raise(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """``health()`` is documented never to raise, and the cycle does not rely on that."""

    adapter = _FakeAdapter("fake", probe_error=RuntimeError("https://host/x?token=SECRET"))
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(adapter))

    outcome = cycle.run_once()[0]

    assert outcome.state == "degraded"
    # The exception message is never quoted, because it can echo a request URL.
    assert "SECRET" not in outcome.detail
    assert "RuntimeError" in outcome.detail
    assert adapter.fetches == 0


def test_a_fetch_that_raises_is_an_error_not_an_empty_observation(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """``fetch_for_ref`` raising is recorded as a failure, never as "fetched nothing"."""

    adapter = _FakeAdapter("fake", fetch_error=_BrokenError("https://host/y?token=SECRET"))
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(adapter))

    outcome = cycle.run_once()[0]

    assert outcome.state == "degraded"
    assert outcome.fetched == 0
    assert "did not reach the adapter" in outcome.detail
    assert "SECRET" not in outcome.detail
    assert "https://host" not in outcome.detail


def test_a_fetch_error_that_is_a_git_ci_error_quotes_the_adapter_message(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """A ``GitCiError``'s message is adapter-authored and queryless, so it is safe to report."""

    from nyx.adapters.git_ci.github_client import GitCiError

    adapter = _FakeAdapter(
        "fake", fetch_error=GitCiError("GitHub CI request to /repos/o/r returned HTTP 404")
    )
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(adapter))

    outcome = cycle.run_once()[0]

    assert outcome.state == "degraded"
    assert "HTTP 404" in outcome.detail


def test_one_failing_adapter_does_not_stop_the_next_one(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """The property ADR 0009 is named for: one failure never kills the cycle."""

    broken = _FakeAdapter("aaa-broken", fetch_error=_BrokenError("boom"))
    healthy = _FakeAdapter("zzz-healthy", inputs=[_event_input("watch-next-1")])
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(broken, healthy))

    outcomes = cycle.run_once()

    assert [outcome.adapter_id for outcome in outcomes] == ["aaa-broken", "zzz-healthy"]
    assert outcomes[0].state == "degraded"
    # The second adapter was still probed, fetched, ingested, and audited.
    assert outcomes[1].state == "ok"
    assert outcomes[1].ingested == 1
    assert healthy.fetches == 1


def test_a_disabled_adapter_is_absent_from_the_cycle(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """Disabled is applied by the registry's filter before the cycle sees the adapter."""

    registry = AdapterRegistry(is_enabled=lambda adapter_id: adapter_id == "fake")
    registry.register(_FakeAdapter("fake", inputs=[_event_input("watch-on-1")]))
    disabled = _FakeAdapter("other")
    registry.register(disabled)
    cycle = _cycle(session_factory, _settings(tmp_path, database), registry)

    outcomes = cycle.run_once()

    assert [outcome.adapter_id for outcome in outcomes] == ["fake"]
    assert disabled.probes == 0
    assert disabled.fetches == 0


def test_a_cycle_without_an_adapter_inventory_refuses_to_start() -> None:
    """A watcher with no adapters would fetch nothing and report success.

    ADR 0009 forbids that, so the constructor names the missing argument rather than defaulting
    to an empty registry. This is the fail-closed property the CLI relies on.
    """

    with pytest.raises(MissingAdapterInventoryError, match="adapter inventory"):
        WatchCycle(
            session_factory=object(),  # type: ignore[arg-type]
            settings=_settings(tmp_path=Path("."), database=Path(".")),
        )


def test_the_outcome_timestamps_are_the_cycle_clock(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """A cycle reports when it started and ended, in UTC, from the injected clock."""

    start = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
    clock = _Clock(start)
    cycle = _cycle(
        session_factory,
        _settings(tmp_path, database),
        _registry(_FakeAdapter("fake", inputs=[_event_input("watch-clock-1")])),
        clock=clock,
    )

    outcome = cycle.run_once()[0]

    assert outcome.started_at == start
    assert outcome.completed_at >= outcome.started_at
    assert outcome.started_at.tzinfo is not None


def test_detail_is_bounded_even_when_the_adapter_reports_a_huge_health_detail(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """A hostile adapter must not be able to overflow the outcome or audit fields.

    ``AdapterHealth.detail`` is capped by its own model at 1000 characters, so the adapter is
    given exactly that much prose. The outcome's ``detail`` must still be bounded, because the
    audit entry's ``reason`` field is capped at 2000 and a longer detail would silently
    truncate the counts an operator reads.
    """

    adapter = _FakeAdapter(
        "fake", inputs=[_event_input("watch-bounded-1")], health_detail="x" * 1000
    )
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(adapter))

    outcome = cycle.run_once()[0]

    # The health prose is capped on its own, so the outcome stays far below the audit field's
    # limit even though the adapter reported the maximum its own model allows.
    assert len(outcome.detail) < 1000 + 200
    assert len(outcome.detail) <= 2000
    # The audit reason is written whole, and it carries the counts.
    reason = _watch_entries(session_factory)[0][3]
    assert f"ingested={outcome.ingested}" in reason
    assert len(reason) <= 2000

    # The bound is on the *whole* detail, not just the prose: a cycle that ingested many records
    # appends one note each, and the summary must still fit the audit field.
    many = _FakeAdapter(
        "fake",
        inputs=[_event_input(f"watch-many-{index}") for index in range(60)],
    )
    many_cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(many))
    many_outcome = many_cycle.run_once()[0]

    assert len(many_outcome.detail) <= 2000
    assert many_outcome.ingested == 60
    many_reason = _watch_entries(session_factory)[-1][3]
    assert f"ingested={many_outcome.ingested}" in many_reason
    assert len(many_reason) <= 2000


def test_detail_carries_no_url_with_a_token(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """A token in a failure must not survive into the outcome the operator reads."""

    token = "ghp_unit_probe_token_000000000000"  # noqa: S105 - test fixture, never a real token
    adapter = _FakeAdapter(
        "fake", fetch_error=_BrokenError(f"https://api.example.com/x?token={token}")
    )
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(adapter))

    outcome = cycle.run_once()[0]

    assert token not in outcome.detail
    assert "api.example.com" not in outcome.detail


# ----------------------------------------------------------------- idempotency


def test_a_second_cycle_over_an_unchanged_ref_is_a_duplicate(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """The property that makes a timer safe: re-fetching the same ref creates no new rows.

    The counts are not asserted here -- the *rows* are. A counter the implementation also
    computes would pass a test that broke the gateway's deduplication; the table will not.
    """

    from nyx.storage.db import session_scope
    from nyx.storage.tables import EventRow

    adapter = _FakeAdapter("fake", inputs=[_event_input("watch-idem-1")])
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(adapter))

    first = cycle.run_once()[0]
    second = cycle.run_once()[0]

    assert first.ingested == 1
    assert first.duplicates == 0
    assert second.ingested == 0
    assert second.duplicates == 1

    with session_scope(session_factory) as session:
        total = int(session.scalar(select(func.count()).select_from(EventRow)) or 0)
    # Exactly one event row for one producer event, across two cycles.
    assert total == 1


def test_a_second_cycle_creates_no_new_finding(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """A duplicate event is not re-criticised, so a timer does not pile up findings."""

    adapter = _FakeAdapter("fake", inputs=[_event_input("watch-find-1")])
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(adapter))

    cycle.run_once()
    second = cycle.run_once()[0]

    # The first cycle ran the pack over the new event; the second had nothing new to assess.
    assert second.findings == 0
    assert second.ingested == 0


# ----------------------------------------------------------------- audit


def test_exactly_one_audit_entry_per_adapter_per_cycle(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """Every cycle appends one ``watch.cycle`` entry per adapter -- including failures."""

    broken = _FakeAdapter("aaa-broken", fetch_error=_BrokenError("boom"))
    healthy = _FakeAdapter("zzz-healthy", inputs=[_event_input("watch-audit-1")])
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(broken, healthy))

    cycle.run_once()
    entries = _watch_entries(session_factory)
    assert len(entries) == 2

    cycle.run_once()
    entries = _watch_entries(session_factory)
    # Two cycles, two adapters each: the count is exactly four, no more and no fewer.
    assert len(entries) == 4

    by_adapter = {reason.split("adapter=")[1].split(" ")[0]: reason for _, _, _, reason in entries}
    assert set(by_adapter) == {"aaa-broken", "zzz-healthy"}


def test_the_audit_actor_is_the_watcher_service_not_a_human(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """An unattended writer must not be recorded as a person."""

    cycle = _cycle(
        session_factory,
        _settings(tmp_path, database),
        _registry(_FakeAdapter("fake", inputs=[_event_input("watch-actor-1")])),
    )

    cycle.run_once()
    entries = _watch_entries(session_factory)

    assert entries
    for _, actor_id, actor_kind, _ in entries:
        assert actor_id == WATCHER_PRINCIPAL.id == "nyx-watch"
        assert actor_kind == "service"


def test_the_audit_reason_names_the_adapter_and_the_counts(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """A reader must be able to reconstruct the cycle from the audit entry alone."""

    cycle = _cycle(
        session_factory,
        _settings(tmp_path, database),
        _registry(_FakeAdapter("fake", inputs=[_event_input("watch-reason-1")])),
    )

    outcome = cycle.run_once()[0]
    _, _, _, reason = _watch_entries(session_factory)[0]

    assert "adapter=fake" in reason
    assert "fetched=1" in reason
    assert "ingested=1" in reason
    assert "duplicates=0" in reason
    assert "ref='main'" in reason
    # The reason matches the outcome it reports, not a counter computed elsewhere.
    assert f"findings={outcome.findings}" in reason


# ----------------------------------------------------------------- cannot judge


def test_the_watcher_never_imports_the_dispute_or_lifecycle_surface() -> None:
    """The executable form of ADR 0009's read-only rule for the watcher module."""

    source = _RUNNER_SOURCE.read_text(encoding="utf-8")
    assert source.strip()

    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    offenders = sorted(
        module
        for module in imported
        if module in FORBIDDEN_MODULES or any(module.startswith(f"{m}.") for m in FORBIDDEN_MODULES)
    )
    assert not offenders, f"the watcher reaches for a judging surface: {offenders}"


def test_a_cycle_creates_no_disposition_or_rebuttal_row(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """Read the tables directly: a cycle that cannot judge writes no judging row."""

    cycle = _cycle(
        session_factory,
        _settings(tmp_path, database),
        _registry(_FakeAdapter("fake", inputs=[_event_input("watch-judge-1")])),
    )

    cycle.run_once()

    assert _count(session_factory, DispositionRow) == 0
    assert _count(session_factory, RebuttalRow) == 0


def test_the_adapter_protocol_is_satisfied_by_the_fake() -> None:
    """The fake is a real ``Adapter``, so these tests are not type-checking a duck."""

    assert isinstance(_FakeAdapter("fake"), Adapter)
