"""Integration tests for the system registry and its audit trail."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from nyx.config import Settings
from nyx.domain.enums import Environment, SystemKind, SystemStatus
from nyx.domain.systems import MonitoredSystem
from nyx.storage.db import (
    create_all,
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from nyx.storage.repositories.audit import AuditRepository
from nyx.storage.repositories.systems import SystemRepository
from nyx.systems.service import get_system, list_systems, register_system


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'registry.sqlite3').as_posix()}",
        data_dir=tmp_path,
    )
    engine = create_engine_from_settings(settings)
    create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _system(name: str = "ghost-factory") -> MonitoredSystem:
    return MonitoredSystem(
        name=name,
        kind=SystemKind.REPOSITORY,
        environment=Environment.STAGING,
        owner="demo-team",
        capabilities=["events.write", "findings.read"],
        adapter="http",
        metadata={"repository": "example/ghost-factory"},
    )


def test_register_get_and_list(engine: Engine) -> None:
    factory = create_session_factory(engine)
    system = _system()

    with session_scope(factory) as session:
        registered = register_system(session, system, request_id="req-register-1")

    assert registered.id == system.id

    with session_scope(factory) as session:
        fetched = get_system(session, system.id)
        systems = list_systems(session)

    assert fetched is not None
    assert fetched.id == system.id
    assert fetched.name == "ghost-factory"
    assert fetched.environment is Environment.STAGING
    assert [item.id for item in systems] == [system.id]


def test_registration_is_idempotent_on_name_and_environment(engine: Engine) -> None:
    factory = create_session_factory(engine)
    first = _system()
    replay = _system()

    with session_scope(factory) as session:
        created = register_system(session, first, request_id="req-first")

    with session_scope(factory) as session:
        replayed = register_system(session, replay, request_id="req-replay")
        repository = SystemRepository(session)
        stored = repository.list()
        audits = AuditRepository(session).list()

    assert replayed.id == created.id
    assert replayed.id != replay.id
    assert len(stored) == 1

    assert len(audits) == 2
    assert audits[0].action == "system.registered"
    assert audits[0].target.type == "monitored_system"
    assert audits[0].target.id == created.id
    assert audits[0].request_id == "req-first"
    assert audits[0].prior_entry_hash is None

    assert audits[1].action == "system.registration_replayed"
    assert audits[1].target.id == created.id
    assert audits[1].request_id == "req-replay"
    assert audits[1].prior_entry_hash == audits[0].entry_hash


def test_session_scope_rolls_back_on_error(engine: Engine) -> None:
    factory = create_session_factory(engine)
    system = _system("rollback-system")

    with pytest.raises(RuntimeError), session_scope(factory) as session:
        register_system(session, system, request_id="req-rollback")
        raise RuntimeError("boom")

    with session_scope(factory) as session:
        assert get_system(session, system.id) is None
        assert AuditRepository(session).list() == []


def test_repositories_do_not_commit_implicitly(engine: Engine) -> None:
    factory = create_session_factory(engine)
    system = _system("uncommitted-system")

    session = factory()
    SystemRepository(session).add(system)
    session.close()

    with session_scope(factory) as verification:
        assert get_system(verification, system.id) is None


def test_list_can_filter_by_status(engine: Engine) -> None:
    factory = create_session_factory(engine)

    with session_scope(factory) as session:
        active = register_system(session, _system("active-system"))
        paused_system = _system("paused-system").model_copy(update={"status": SystemStatus.PAUSED})
        paused = register_system(session, paused_system)

    with session_scope(factory) as session:
        active_only = list_systems(session, status=SystemStatus.ACTIVE)
        paused_only = list_systems(session, status=SystemStatus.PAUSED)

    assert [item.id for item in active_only] == [active.id]
    assert [item.id for item in paused_only] == [paused.id]
