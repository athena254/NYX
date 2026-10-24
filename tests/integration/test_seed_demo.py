"""H5: `nyx seed demo` loads the Ghost Factory demonstration idempotently.

The property that matters is not "the seed runs" but "the seed is safe to run twice". A seed that
appends rows on every invocation is a demonstration that quietly corrupts the store it is meant to
illustrate, and it is the property a Makefile or an operator would rely on without reading the code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nyx.cli.main import app
from nyx.config import Settings
from nyx.seed.demo import DEFAULT_FIXTURE_DIR, SEED_ORDER, SeedError, seed_demo
from nyx.storage.db import create_engine_from_settings, create_session_factory, session_scope
from nyx.storage.repositories.audit import AuditRepository
from nyx.storage.repositories.claims import ClaimRepository
from nyx.storage.repositories.events import EventRepository
from nyx.storage.repositories.systems import SystemRepository

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _env(tmp_path: Path, database: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    return env


def _record_counts(settings: Settings) -> tuple[int, int, int]:
    """Systems, events, claims -- the records the seed writes.

    Audit entries are deliberately excluded: each accepted or replayed record appends one, so the
    audit trail *grows* on a replay by design. That is correct -- an unattended replay is exactly
    the kind of thing an append-only trail should record -- and it is not what "idempotent" means
    here. Idempotent means the store's records do not change.
    """

    factory = create_session_factory(create_engine_from_settings(settings))
    try:
        with session_scope(factory) as session:
            return (
                len(SystemRepository(session).list(limit=100, offset=0)),
                len(EventRepository(session).list(limit=100, offset=0)),
                len(ClaimRepository(session).list(limit=100, offset=0)),
            )
    finally:
        factory.kw["bind"].dispose()


def test_seed_fixtures_exist_and_are_named() -> None:
    """The seed's declared order must match the directory on disk, or the seed is a guess."""

    assert DEFAULT_FIXTURE_DIR.is_dir()
    on_disk = {path.name for path in DEFAULT_FIXTURE_DIR.glob("*.json")}
    assert set(SEED_ORDER) == on_disk, (
        f"SEED_ORDER and the fixture directory disagree: {sorted(on_disk ^ set(SEED_ORDER))}"
    )


def test_seed_loads_every_fixture_once(tmp_path: Path, database: Path) -> None:
    """Every declared fixture is ingested, and nothing is rejected."""

    settings = Settings(database_url=f"sqlite:///{database.as_posix()}", data_dir=tmp_path)
    outcome = seed_demo(settings, fixture_dir=DEFAULT_FIXTURE_DIR)

    assert outcome.ok is True
    assert outcome.rejected == []
    assert sorted(outcome.created) == sorted(SEED_ORDER)
    assert outcome.duplicates == []


def test_a_second_seed_run_is_a_no_op(tmp_path: Path, database: Path) -> None:
    """Re-running the seed must not grow any table. This is the property a script relies on."""

    settings = Settings(database_url=f"sqlite:///{database.as_posix()}", data_dir=tmp_path)
    seed_demo(settings, fixture_dir=DEFAULT_FIXTURE_DIR)
    counts_after_first = _record_counts(settings)

    second = seed_demo(settings, fixture_dir=DEFAULT_FIXTURE_DIR)

    assert second.created == []
    assert sorted(second.duplicates) == sorted(SEED_ORDER)
    assert _record_counts(settings) == counts_after_first, "the second seed run grew a table"
    assert counts_after_first[0] >= 1, "the seed did not register its system"


def test_the_seed_writes_through_the_gateway_not_past_it(tmp_path: Path, database: Path) -> None:
    """The audit trail is the proof the seed used the ordinary ingestion path."""

    settings = Settings(database_url=f"sqlite:///{database.as_posix()}", data_dir=tmp_path)
    seed_demo(settings, fixture_dir=DEFAULT_FIXTURE_DIR)

    factory = create_session_factory(create_engine_from_settings(settings))
    try:
        with session_scope(factory) as session:
            actions = {
                entry.action for entry in AuditRepository(session).list(limit=1000, offset=0)
            }
    finally:
        factory.kw["bind"].dispose()

    # The gateway audits each accepted record. A seed that wrote rows directly would have none.
    assert "system.registered" in actions
    assert "event.ingested" in actions or "event.recorded" in actions


def test_the_seed_never_judges(tmp_path: Path, database: Path) -> None:
    """A demonstration ingests and stops: no critic ran, so no finding exists."""

    settings = Settings(database_url=f"sqlite:///{database.as_posix()}", data_dir=tmp_path)
    seed_demo(settings, fixture_dir=DEFAULT_FIXTURE_DIR)

    factory = create_session_factory(create_engine_from_settings(settings))
    try:
        with session_scope(factory) as session:
            from sqlalchemy import text

            findings = session.execute(text("SELECT COUNT(*) FROM findings")).scalar_one()
            critic_runs = session.execute(text("SELECT COUNT(*) FROM critic_runs")).scalar_one()
    finally:
        factory.kw["bind"].dispose()

    assert findings == 0, "the seed must not produce findings"
    assert critic_runs == 0, "the seed must not run a critic"


def test_the_cli_command_is_idempotent_and_json_clean(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`nyx seed demo --json` emits one JSON document and exits 0 on a repeat run."""

    monkeypatch.chdir(REPOSITORY_ROOT)
    runner = CliRunner()
    env = _env(tmp_path, database)

    first = runner.invoke(app, ["seed", "demo", "--json"], env=env)
    assert first.exit_code == 0, first.output
    payload = json.loads(first.stdout)
    assert payload["ok"] is True
    assert payload["rejected"] == []

    second = runner.invoke(app, ["seed", "demo", "--json"], env=env)
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["created"] == []


def test_an_unknown_seed_is_refused(tmp_path: Path, database: Path, monkeypatch) -> None:
    """A typo is an invalid request with a code, not a traceback and not a silent success."""

    monkeypatch.chdir(REPOSITORY_ROOT)
    runner = CliRunner()

    result = runner.invoke(app, ["seed", "prod", "--json"], env=_env(tmp_path, database))

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    payload = json.loads(result.output)
    assert payload["code"] == "INVALID_REQUEST"
    assert "prod" in payload["message"]


def test_a_missing_fixture_directory_is_refused(tmp_path: Path, database: Path) -> None:
    """A seed that cannot find its fixtures reports a failure, never a vacuous success."""

    settings = Settings(database_url=f"sqlite:///{database.as_posix()}", data_dir=tmp_path)

    with pytest.raises(SeedError) as excinfo:
        seed_demo(settings, fixture_dir=tmp_path / "absent")

    assert excinfo.value.code == "SEED_FIXTURE_DIR_MISSING"


def test_a_fixture_that_is_not_an_object_is_refused(tmp_path: Path, database: Path) -> None:
    """A JSON array where an object belongs is a malformed fixture, not an empty record."""

    settings = Settings(database_url=f"sqlite:///{database.as_posix()}", data_dir=tmp_path)
    broken = tmp_path / "broken"
    broken.mkdir()
    for name in SEED_ORDER:
        (broken / name).write_text("[]", encoding="utf-8")

    with pytest.raises(SeedError) as excinfo:
        seed_demo(settings, fixture_dir=broken)

    assert excinfo.value.code == "SEED_FIXTURE_INVALID"


def test_a_fixture_of_an_unknown_type_is_refused(tmp_path: Path, database: Path) -> None:
    """The seed must not guess a record type: an unrecognisable fixture is a failure."""

    settings = Settings(database_url=f"sqlite:///{database.as_posix()}", data_dir=tmp_path)
    odd = tmp_path / "odd"
    odd.mkdir()
    for name in SEED_ORDER:
        (odd / name).write_text('{"something_else": 1}', encoding="utf-8")

    with pytest.raises(SeedError) as excinfo:
        seed_demo(settings, fixture_dir=odd)

    assert excinfo.value.code == "SEED_FIXTURE_UNKNOWN_TYPE"
