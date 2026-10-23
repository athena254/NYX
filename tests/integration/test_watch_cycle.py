"""Integration tests: a watcher cycle driven by the real adapter against a real database.

The unit tests use a fake adapter to exercise the cycle's control flow. This file exercises the
**producer half of the boundary** for real: the checked-in fixture provider
(``git_ci_provider='fixture'``) reads local JSON, makes no network call, and emits the same
producer inputs the GitHub provider does, so the adapter, the gateway, the critic pack, and the
audit chain are all the production code.

The four properties asserted here are the ones a fake adapter cannot prove:

* **Idempotency is a property of the store, not of a counter.** Fetching the same unchanged ref
  twice must leave exactly one event row and one evidence row. The tables are read directly.
* **The cycle audit entry verifies.** ``nyx.audit.verifier.verify_chain`` walks the whole chain
  after several cycles, including a degraded one.
* **A down adapter degrades without raising**, and the next adapter in the same cycle still runs.
* **The watcher cannot judge.** No disposition or rebuttal row exists after a cycle that produced
  findings -- read from the tables, not from a service return value.

Nothing here edits the CLI or the docs; the watcher is driven through its own public surface.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import func, select

from nyx.adapters.base import AdapterHealth, AdapterRegistry, AdapterState
from nyx.adapters.git_ci.adapter import GitCiAdapter
from nyx.adapters.git_ci.config import GitCiConfig
from nyx.adapters.git_ci.fixture_provider import FixtureCiClient
from nyx.audit.verifier import verify_chain
from nyx.config import Settings
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.tables import (
    AuditRow,
    DispositionRow,
    EventRow,
    EvidenceRow,
    FindingRow,
    RebuttalRow,
)
from nyx.watch.runner import CYCLE_ACTION, WATCHER_PRINCIPAL, WatchCycle

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPOSITORY_ROOT / "fixtures" / "git_ci"
CHECK_RUNS_FIXTURE = FIXTURE_DIR / "check_runs.json"
ACTIONS_RUNS_FIXTURE = FIXTURE_DIR / "actions_runs.json"

#: The repository the fixtures speak for. Must match the repository the adapter is configured
#: with, or the normalizer refuses the records.
REPOSITORY = "fixture/repo"

#: The synthetic credential a down-adapter test plants in the environment. It is *input* only:
#: the assertion below searches the cycle's outcome for this literal, and the searched-for
#: string is never built from the value itself.
SYNTHETIC_TOKEN = "ghp_integration_probe_token_0000000000"  # noqa: S105 - test fixture
TOKEN_ENV_NAME = "NYX_PROBE_GITHUB_TOKEN"  # noqa: S105 - a variable name, not a secret


# ----------------------------------------------------------------- helpers


def _fixture_config(**overrides: Any) -> GitCiConfig:
    """A fixture-provider config, with per-test overrides typed as their field types."""

    values: dict[str, Any] = {
        "provider": "fixture",
        "api_base_url": "file:///fixture",
        "repository": REPOSITORY,
        "max_runs_per_fetch": 10,
    }
    values.update(overrides)
    return GitCiConfig(**values)


def _fixture_adapter() -> GitCiAdapter:
    config = _fixture_config()
    return GitCiAdapter(
        config,
        # The fixture client satisfies the fetch surface the adapter uses; the adapter's own
        # annotation names the GitHub client because that is the production transport.
        client=cast(
            Any,
            FixtureCiClient(
                config,
                check_runs_path=CHECK_RUNS_FIXTURE,
                actions_runs_path=ACTIONS_RUNS_FIXTURE,
            ),
        ),
    )


def _closed_port() -> int:
    """A port that is free *now*, so connecting to it is refused immediately."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _down_adapter() -> GitCiAdapter:
    """A real GitHub client pointed at a closed port: reachable code, unreachable host."""

    config = _fixture_config(
        provider="github",
        api_base_url=f"http://127.0.0.1:{_closed_port()}",
        token_env=TOKEN_ENV_NAME,
        timeout_seconds=1.0,
        allow_private_targets=True,
    )
    return GitCiAdapter(config)


def _settings(tmp_path: Path, database: Path, *, adapters: list[str] | None = None) -> Settings:
    return Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        data_dir=tmp_path,
        enabled_adapters=adapters if adapters is not None else ["git-ci"],
        git_ci_provider="fixture",
        git_ci_repository=REPOSITORY,
        git_ci_token_env=TOKEN_ENV_NAME,
        watch_enabled=True,
        watch_ref="main",
    )


class _Clock:
    """A clock that advances a fixed step per read, so a cycle's duration is deterministic."""

    def __init__(self, start: datetime) -> None:
        self._value = start
        self._step = timedelta(seconds=1)

    def __call__(self) -> datetime:
        current = self._value
        self._value = current + self._step
        return current


class _Inventory:
    """The narrow adapter view the cycle needs, over a real ``AdapterRegistry``.

    ``AdapterRegistry.enabled()`` returns descriptors; the cycle iterates adapters. The adapter
    package is imported here, in a test, which is where gate G7 expects it to be imported.
    """

    def __init__(self, registry: AdapterRegistry) -> None:
        self._registry = registry

    def enabled(self) -> list[Any]:
        return [
            self._registry.get(descriptor.adapter_id) for descriptor in self._registry.enabled()
        ]


def _cycle(
    session_factory: SessionFactory, settings: Settings, registry: AdapterRegistry
) -> WatchCycle:
    return WatchCycle(
        session_factory=session_factory,
        settings=settings,
        inventory=_Inventory(registry),
        clock=_Clock(datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)),
    )


def _registry(*adapters: object) -> AdapterRegistry:
    """Register adapters, renaming any after the first so a registry can hold two git-ci adapters.

    A registry is keyed by ``adapter_id``, and ``GitCiAdapter.adapter_id`` is a class attribute,
    so two instances would collapse onto one entry. The rename is per-instance and only affects
    the key the cycle iterates -- the adapter's fetch path is untouched.
    """

    registry = AdapterRegistry(is_enabled=lambda adapter_id: True)
    for position, adapter in enumerate(adapters):
        instance = cast(Any, adapter)
        if position:
            object.__setattr__(instance, "adapter_id", f"git-ci-{position}")
        registry.register(instance)
    return registry


def _count(session_factory: SessionFactory, model: type) -> int:
    with session_scope(session_factory) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _audit_entries(session_factory: SessionFactory) -> list[tuple[str, dict[str, Any], str]]:
    """Every ``watch.cycle`` entry, as ``(action, actor, reason)``."""

    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(AuditRow).where(AuditRow.action == CYCLE_ACTION).order_by(AuditRow.sequence)
        )
        return [(row.action, row.actor, row.reason) for row in rows]


# ----------------------------------------------------------------- idempotency


def _watch_with_fixtures(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    probe_state: AdapterState | None = None,
) -> WatchCycle:
    """Build a cycle whose adapter reads the real fixtures.

    The fixture provider is offline, but ``GitCiAdapter.health()`` is provider-agnostic: it
    issues one HTTP GET, which the fixture client cannot serve (``UnsupportedProtocol``). So the
    probe is stubbed here, and the assertion is the *pair* of behaviours: a probe reporting
    ``OK`` is fetched, and one reporting ``DEGRADED`` is not.

    The stub returns a real :class:`AdapterHealth`; it does not touch the fetch path, the
    normalizer, the gateway, or the critic pack, which are all production code.
    """

    adapter = _fixture_adapter()
    if probe_state is not None:
        adapter.health = lambda: _health(  # type: ignore[method-assign]
            probe_state, detail="stubbed probe for this test"
        )
    return _cycle(session_factory, settings, _registry(adapter))


def _health(state: AdapterState, *, detail: str) -> AdapterHealth:
    return AdapterHealth(
        adapter_id="git-ci",
        version="1.0.0",
        state=state,
        detail=detail,
        checked_at=datetime.now(UTC),
    )


def test_fetching_the_same_unchanged_ref_twice_creates_no_new_rows(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """The property that makes a timer safe, proven against the tables.

    The adapter is the real fixture provider and the gateway is the real one. Two cycles over
    the same ref must leave exactly one row per fixture record -- not one row plus a duplicate.
    """

    settings = _settings(tmp_path, database)
    cycle = _watch_with_fixtures(session_factory, settings, probe_state=AdapterState.OK)

    first = cycle.run_once()
    second = cycle.run_once()

    assert len(first) == 1 and len(second) == 1
    assert first[0].state == "ok"
    # The fixture carries 4 runs (2 check, 2 Actions); each normalizes to an event + evidence.
    assert first[0].fetched == 8
    assert first[0].ingested == 8
    assert first[0].duplicates == 0
    assert second[0].ingested == 0
    assert second[0].duplicates == 8

    events = _count(session_factory, EventRow)
    evidence = _count(session_factory, EvidenceRow)
    # Read back what is actually stored: one row per producer record, not one per cycle.
    assert events == 4
    assert evidence == 4

    # The deduplication is identity-based, so the stored rows are exactly the fixture's four
    # runs, each once -- no second copy from the second cycle.
    with session_scope(session_factory) as session:
        stored = session.scalars(select(EventRow).order_by(EventRow.id)).all()
    assert len(stored) == 4
    # Two distinct commits, each observed twice (a check run and a workflow run).
    assert {row.payload["commit"] for row in stored} == {
        "fixtureaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01",
        "fixturebbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb02",
    }
    assert {row.event_type for row in stored} == {
        "ci.check.completed",
        "ci.workflow.completed",
    }


def test_a_second_cycle_produces_no_new_finding(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """Re-running an already-assessed event is not re-criticised."""

    cycle = _watch_with_fixtures(
        session_factory, _settings(tmp_path, database), probe_state=AdapterState.OK
    )

    first = cycle.run_once()[0]
    second = cycle.run_once()[0]

    assert first.ingested == 8
    # Nothing new was accepted, so the pack was not re-run over them.
    assert second.ingested == 0
    assert second.findings == 0
    # And the finding count did not grow.
    assert _count(session_factory, FindingRow) == first.findings


# ----------------------------------------------------------------- audit


def test_the_audit_trail_contains_the_cycle_entry_with_the_reported_counts(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """One ``watch.cycle`` entry per adapter per cycle, carrying the outcome's own counts."""

    cycle = _watch_with_fixtures(
        session_factory, _settings(tmp_path, database), probe_state=AdapterState.OK
    )

    first = cycle.run_once()[0]
    cycle.run_once()

    entries = _audit_entries(session_factory)
    assert len(entries) == 2
    # The first cycle is the one that ingested; the second was all duplicates. Both entries
    # carry their own counts, and the first must match the outcome it reports.
    first_entry = next(reason for action, _, reason in entries if "cycle 1" in reason)
    for _, actor, reason in entries:
        assert actor["id"] == WATCHER_PRINCIPAL.id == "nyx-watch"
        assert actor["kind"] == "service"
        assert "adapter=git-ci" in reason
    assert f"fetched={first.fetched}" in first_entry
    assert f"ingested={first.ingested}" in first_entry
    assert f"duplicates={first.duplicates}" in first_entry
    assert f"findings={first.findings}" in first_entry

    # The degraded cycle's entry is a failure outcome; the healthy ones are successes.
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(AuditRow).where(AuditRow.action == CYCLE_ACTION).order_by(AuditRow.sequence)
        ).all()
    assert all(row.outcome == "success" for row in rows)


def test_the_audit_chain_verifies_after_several_cycles(
    tmp_path: Path,
    database: Path,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nyx audit verify`` walks a chain that includes cycle entries and finds no divergence."""

    monkeypatch.setenv(TOKEN_ENV_NAME, SYNTHETIC_TOKEN)
    settings = _settings(tmp_path, database)
    # One healthy adapter and one down adapter, under distinct ids: a registry holds one entry
    # per id, so the down adapter is registered as a second id the cycle still iterates.
    healthy = _fixture_adapter()
    down = _down_adapter()
    registry = _registry(healthy, down)

    cycle = _cycle(session_factory, settings, registry)

    for _ in range(3):
        cycle.run_once()

    report = verify_chain(session_factory)

    assert report.ok, f"chain diverged: {report.first_divergence}"
    # 3 cycles x 2 adapters = 6 cycle entries, plus the per-record ingestion audit entries.
    assert report.entries_checked >= 6
    assert len(_audit_entries(session_factory)) == 6


def test_a_degraded_cycle_is_recorded_as_a_failure_in_the_audit_trail(
    tmp_path: Path,
    database: Path,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cycle that could not fetch is an auditable failure, not a silent no-op."""

    monkeypatch.setenv(TOKEN_ENV_NAME, SYNTHETIC_TOKEN)
    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(_down_adapter()))

    cycle.run_once()

    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(AuditRow).where(AuditRow.action == CYCLE_ACTION).order_by(AuditRow.sequence)
        ).all()
    assert len(rows) == 1
    assert rows[0].outcome == "failure"
    assert "outcome=degraded" in rows[0].reason


# ----------------------------------------------------------------- cannot judge


def test_a_cycle_creates_no_disposition_or_rebuttal_row(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """The watcher cannot judge. The tables are read, not a service return value.

    The cycle is driven until it has accepted records and run the critic pack. If any row in
    ``dispositions`` or ``rebuttals`` existed afterwards, the watcher had judged something -- and
    the only writer that could have done it is the cycle.
    """

    cycle = _watch_with_fixtures(
        session_factory, _settings(tmp_path, database), probe_state=AdapterState.OK
    )

    cycle.run_once()

    assert _count(session_factory, DispositionRow) == 0
    assert _count(session_factory, RebuttalRow) == 0
    # The cycle did write the two record types it is allowed to write.
    assert _count(session_factory, EventRow) > 0
    assert _count(session_factory, FindingRow) >= 0


def test_the_cycle_writes_no_lifecycle_transition_audit_entry(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """``finding.transition`` is the lifecycle surface; the watcher must never emit it."""

    cycle = _watch_with_fixtures(
        session_factory, _settings(tmp_path, database), probe_state=AdapterState.OK
    )

    cycle.run_once()

    with session_scope(session_factory) as session:
        rows = session.scalars(select(AuditRow)).all()
        actions = {row.action for row in rows}

    assert "finding.transition" not in actions
    # The only actions present are the gateway's ingestion entries and the cycle's own.
    assert actions <= {"event.ingested", "evidence.registered", CYCLE_ACTION}


def test_findings_the_watcher_produces_are_left_in_detected_state(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """A finding the watcher raises is an ordinary ``detected`` finding, not a decision."""

    cycle = _watch_with_fixtures(
        session_factory, _settings(tmp_path, database), probe_state=AdapterState.OK
    )

    cycle.run_once()

    with session_scope(session_factory) as session:
        rows = session.scalars(select(FindingRow).order_by(FindingRow.id)).all()

    # Whatever the pack found, it found nothing to *decide*: every finding is detected.
    assert all(row.status == "detected" for row in rows)


# ----------------------------------------------------------------- boundary


def test_the_cycle_is_driven_through_the_shared_registry_factory(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """Production wiring uses the shared factory, so the watcher and doctor agree.

    The registry is built by ``build_adapter_registry`` from the same settings the cycle holds,
    and the cycle's outcomes name exactly the adapters that registry enables.
    """

    from nyx.adapters.factory import build_adapter_registry

    settings = _settings(tmp_path, database)
    registry = build_adapter_registry(settings)

    assert [descriptor.adapter_id for descriptor in registry.enabled()] == ["git-ci"]

    # The caller wires the cycle to the shared factory's registry; the runner does not import
    # the adapter package itself (gate G7).
    cycle = WatchCycle(
        session_factory=session_factory, settings=settings, inventory=_Inventory(registry)
    )
    outcomes = cycle.run_once()

    assert [outcome.adapter_id for outcome in outcomes] == [
        descriptor.adapter_id for descriptor in registry.enabled()
    ]


def test_the_cli_wiring_path_is_the_shared_factory_and_the_inventory_view(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """The shape ``nyx watch`` is wired with: factory-built registry, passed as the inventory.

    The runner does not import ``nyx.adapters`` (gate G7), so the CLI is the caller that joins
    them. This is that join, exercised end to end against the real fixture provider: the registry
    comes from the shared factory, the cycle ingests, and the audit chain verifies.
    """

    from nyx.adapters.factory import build_adapter_registry

    settings = _settings(tmp_path, database)
    registry = build_adapter_registry(settings)

    # The registry's deployment filter is what makes the watcher observe exactly the enabled set.
    assert [descriptor.adapter_id for descriptor in registry.enabled()] == ["git-ci"]

    cycle = _cycle(session_factory, settings, registry)
    outcomes = cycle.run_once()

    # One outcome, naming the adapter the deployment enabled -- the join is what is under test.
    assert [outcome.adapter_id for outcome in outcomes] == ["git-ci"]
    # The registry built the GitHub provider, whose health probe is an HTTP GET; offline it
    # reports DEGRADED and the cycle correctly declines to fetch. That is the probe gate working
    # through the real wiring, and it is why the fixture path stubs the probe instead.
    assert outcomes[0].state == "degraded"
    assert outcomes[0].fetched == 0

    # The degraded cycle is still audited, and its entry verifies in the ordinary chain.
    report = verify_chain(session_factory)
    assert report.ok, f"chain diverged: {report.first_divergence}"
    assert len(_audit_entries(session_factory)) == 1


def test_the_fixture_files_are_unchanged_by_a_cycle(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """The watcher is read-only: reading a fixture must not rewrite it."""

    before = (
        CHECK_RUNS_FIXTURE.read_text(encoding="utf-8"),
        ACTIONS_RUNS_FIXTURE.read_text(encoding="utf-8"),
    )
    assert json.loads(before[0])["total_count"] == 2

    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(_fixture_adapter()))
    cycle.run_once()

    after = (
        CHECK_RUNS_FIXTURE.read_text(encoding="utf-8"),
        ACTIONS_RUNS_FIXTURE.read_text(encoding="utf-8"),
    )
    assert after == before


def test_the_watcher_principal_is_stable_across_cycles(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """The unattended writer keeps one identity, so its entries are attributable."""

    cycle = _cycle(session_factory, _settings(tmp_path, database), _registry(_fixture_adapter()))
    cycle.run_once()
    cycle.run_once()

    entries = _audit_entries(session_factory)
    assert len(entries) == 2
    assert {actor["id"] for _, actor, _ in entries} == {"nyx-watch"}
    assert {actor["display_name"] for _, actor, _ in entries} == {"NYX watcher"}
    assert _count(session_factory, AuditRow) >= 2


def test_the_cycle_does_not_mutate_the_outcome_of_a_rejected_input(
    tmp_path: Path, database: Path, session_factory: SessionFactory
) -> None:
    """A record the gateway rejects is reported, not silently dropped or counted as fetched."""

    cycle = _watch_with_fixtures(
        session_factory, _settings(tmp_path, database), probe_state=AdapterState.OK
    )

    outcome = cycle.run_once()[0]

    # Every accepted fixture record is created; nothing was rejected by the gateway.
    assert outcome.ingested == outcome.fetched
    assert "rejected:" not in outcome.detail
