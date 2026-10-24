"""H3 (ADR 0012): external audit checkpoints detect a fully recomputed rewrite.

The chain verifier alone cannot detect an actor who rewrites the whole database and recomputes
every hash. These tests prove the one new guarantee a checkpoint buys, and -- as importantly --
that a missing, short, or malformed checkpoint is a **refusal** rather than a silent pass.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from nyx.audit.checkpoint import (
    REASON_MISMATCH,
    REASON_NO_CHECKPOINT,
    REASON_SEQUENCE_BEYOND_CHAIN,
    REASON_TRUNCATED,
    CheckpointError,
    CheckpointRecord,
    compare_against_checkpoint,
    write_checkpoint,
)
from nyx.audit.writer import AuditEntryInput, AuditWriter
from nyx.domain.base import PrincipalRef
from nyx.domain.enums import AuditOutcome, PrincipalKind
from nyx.storage.db import create_engine_from_settings, create_session_factory, session_scope

_ACTOR = PrincipalRef(id="principal_operator", kind=PrincipalKind.HUMAN)


def _entry(
    sequence: int,
    *,
    timestamp: datetime,
    action: str = "system.registered",
    reason: str = "checkpoint test entry",
) -> AuditEntryInput:
    return AuditEntryInput(
        schema_version="1.0",
        actor=_ACTOR,
        action=action,
        target_type="system",
        target_id=f"sys_checkpoint_{sequence}",
        timestamp=timestamp,
        request_id=f"checkpoint-test-{sequence}",
        correlation_id=None,
        outcome=AuditOutcome.SUCCESS,
        reason=f"{reason} {sequence}",
    )


def _write_entries(factory, count: int) -> None:
    from nyx.storage.db import session_scope

    base = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
    with session_scope(factory) as session:
        writer = AuditWriter(session)
        for index in range(1, count + 1):
            writer.append(_entry(index, timestamp=base.replace(second=index)))


def _migrate(factory) -> None:
    """Bring the store to head, as the CLI does before any command reads it.

    Uses ``migrations/env.py`` rather than a hand-built Alembic config: the URL goes through
    ``Settings``, which escapes ``%`` for ``configparser``. A hand-built config that reads the URL
    straight off the engine dies on the ``%3A`` of a URL-encoded Windows path.
    """

    root = Path(__file__).resolve().parents[2]
    database = factory.kw["bind"].url.database or ""
    previous = os.environ.get("NYX_DATABASE_URL")
    os.environ["NYX_DATABASE_URL"] = str(factory.kw["bind"].url)
    os.environ["NYX_DATA_DIR"] = str(Path(database).parent)
    try:
        from alembic.config import main as alembic_main

        alembic_main(["-c", str(root / "alembic.ini"), "upgrade", "head"], prog="pytest-alembic")
    finally:
        if previous is None:
            os.environ.pop("NYX_DATABASE_URL", None)
        else:
            os.environ["NYX_DATABASE_URL"] = previous


@pytest.fixture()
def factory(tmp_path: Path):
    engine = create_engine_from_settings(_settings(tmp_path))
    built = create_session_factory(engine)
    _migrate(built)
    return built


@pytest.fixture()
def chain_factory(tmp_path: Path):
    """A store with three entries, migrated."""

    built = factory.__wrapped__(tmp_path) if hasattr(factory, "__wrapped__") else _factory(tmp_path)
    _migrate(built)
    _write_entries(built, 3)
    return built


def _factory(tmp_path: Path):
    engine = create_engine_from_settings(_settings(tmp_path))
    return create_session_factory(engine)


def _settings(tmp_path: Path):
    from nyx.config import Settings

    return Settings(
        database_url=f"sqlite:///{tmp_path.as_posix()}/checkpoints.sqlite3",
        data_dir=tmp_path,
    )


# --------------------------------------------------------------------------- writing


def test_write_checkpoint_anchors_the_current_head(chain_factory, tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.jsonl"

    record = write_checkpoint(chain_factory, path=path)

    assert record.sequence == 3
    assert record.entries_checked == 3
    assert record.head_hash.startswith("sha256:")
    assert len(record.head_hash) == len("sha256:") + 64
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record.as_dict()


def test_second_checkpoint_appends_rather_than_replacing(chain_factory, tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.jsonl"

    first = write_checkpoint(chain_factory, path=path)
    _write_entries(chain_factory, 2)
    second = write_checkpoint(chain_factory, path=path)

    assert first.sequence == 3
    assert second.sequence == 5
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_write_checkpoint_creates_a_missing_parent_directory(chain_factory, tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "checkpoints.jsonl"

    write_checkpoint(chain_factory, path=path)

    assert path.exists()


def test_write_checkpoint_refuses_an_empty_chain(factory, tmp_path: Path) -> None:
    """A checkpoint of nothing would verify vacuously; refusing is the honest behaviour."""

    path = tmp_path / "checkpoints.jsonl"

    with pytest.raises(CheckpointError) as excinfo:
        write_checkpoint(factory, path=path)

    assert excinfo.value.code == REASON_NO_CHECKPOINT
    assert not path.exists()


def test_write_checkpoint_refuses_an_unwritable_path(chain_factory, tmp_path: Path) -> None:
    """A checkpoint that was not written must be an error, not a no-op that looks like success."""

    path = tmp_path / "a-file" / "checkpoints.jsonl"
    (tmp_path / "a-file").write_text("not a directory", encoding="utf-8")

    with pytest.raises(CheckpointError) as excinfo:
        write_checkpoint(chain_factory, path=path)

    assert excinfo.value.code == "CHECKPOINT_UNWRITABLE"


# --------------------------------------------------------------------------- comparing


def test_matching_checkpoint_verifies(chain_factory, tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.jsonl"
    record = write_checkpoint(chain_factory, path=path)

    comparison = compare_against_checkpoint(chain_factory, path=path)

    assert comparison.ok is True
    assert comparison.reason == "checkpoint_verified"
    assert comparison.checkpoint_head_hash == record.head_hash
    assert comparison.stored_head_hash == record.head_hash


def test_a_recomputed_rewrite_no_longer_verifies(chain_factory, tmp_path: Path) -> None:
    """The whole point: an internally consistent rewrite is caught by the external anchor."""

    path = tmp_path / "checkpoints.jsonl"
    write_checkpoint(chain_factory, path=path)

    # Rewrite history: same shape, different reason. The chain recomputes cleanly, so `nyx audit
    # verify` still reports ok -- only the external anchor can catch it.
    base = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
    with session_scope(chain_factory) as session:
        session.execute(text("DELETE FROM audit_entries"))
        writer = AuditWriter(session)
        for index in range(1, 4):
            writer.append(
                _entry(
                    index,
                    timestamp=base.replace(second=index),
                    reason="rewritten by an actor who recomputed every hash",
                )
            )

    from nyx.audit.verifier import verify_chain

    assert verify_chain(chain_factory).ok is True, "the rewrite must be internally consistent"

    comparison = compare_against_checkpoint(chain_factory, path=path)

    assert comparison.ok is False
    assert comparison.reason == REASON_MISMATCH
    assert comparison.stored_head_hash != comparison.checkpoint_head_hash


def test_missing_checkpoint_file_is_a_refusal(chain_factory, tmp_path: Path) -> None:
    comparison = compare_against_checkpoint(chain_factory, path=tmp_path / "absent.jsonl")

    assert comparison.ok is False
    assert comparison.reason == REASON_NO_CHECKPOINT


def test_empty_checkpoint_file_is_a_refusal(chain_factory, tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.jsonl"
    path.write_text("", encoding="utf-8")

    comparison = compare_against_checkpoint(chain_factory, path=path)

    assert comparison.ok is False
    assert comparison.reason == REASON_NO_CHECKPOINT


def test_a_chain_truncated_below_the_checkpoint_is_a_refusal(chain_factory, tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.jsonl"
    write_checkpoint(chain_factory, path=path)

    with session_scope(chain_factory) as session:
        from nyx.storage.tables import AuditRow

        row = session.execute(
            text("SELECT id FROM audit_entries ORDER BY sequence DESC LIMIT 1")
        ).fetchone()
        assert row is not None
        session.delete(session.get(AuditRow, row[0]))

    comparison = compare_against_checkpoint(chain_factory, path=path)

    assert comparison.ok is False
    # Deleting the newest row leaves the chain shorter than the anchor; deleting a middle row
    # leaves a gap. Both are refusals, and both must be named: the reason says which.
    assert comparison.reason in {REASON_TRUNCATED, REASON_SEQUENCE_BEYOND_CHAIN}
    assert comparison.reason != "checkpoint_verified"


def test_a_checkpoint_ahead_of_the_chain_is_a_refusal(chain_factory, tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.jsonl"
    write_checkpoint(chain_factory, path=path)

    # A checkpoint from a store that had more entries than this one: the operator is being asked to
    # vouch for history that is not here.
    ahead = tmp_path / "ahead.jsonl"
    ahead.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sequence": 9,
                "head_hash": "sha256:" + "a" * 64,
                "recorded_at": "2026-10-24T00:00:00Z",
                "entries_checked": 9,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    comparison = compare_against_checkpoint(chain_factory, path=ahead)

    assert comparison.ok is False
    assert comparison.reason == REASON_SEQUENCE_BEYOND_CHAIN


@pytest.mark.parametrize(
    "line",
    [
        '{"schema_version": "1.0", "sequence": 1}',
        json.dumps(
            {
                "schema_version": "9.9",
                "sequence": 1,
                "head_hash": "sha256:" + "a" * 64,
                "recorded_at": "2026-10-24T00:00:00Z",
                "entries_checked": 1,
            }
        ),
        json.dumps(
            {
                "schema_version": "1.0",
                "sequence": 0,
                "head_hash": "sha256:" + "a" * 64,
                "recorded_at": "2026-10-24T00:00:00Z",
                "entries_checked": 1,
            }
        ),
        json.dumps(
            {
                "schema_version": "1.0",
                "sequence": 1,
                "head_hash": "too-short",
                "recorded_at": "2026-10-24T00:00:00Z",
                "entries_checked": 1,
            }
        ),
        "not json at all",
    ],
)
def test_a_malformed_checkpoint_line_is_a_refusal(chain_factory, tmp_path: Path, line: str) -> None:
    path = tmp_path / "checkpoints.jsonl"
    path.write_text(f"{line}\n", encoding="utf-8")

    with pytest.raises(CheckpointError) as excinfo:
        compare_against_checkpoint(chain_factory, path=path)

    assert excinfo.value.code == "malformed_checkpoint"


def test_the_latest_record_wins_when_several_are_present(chain_factory, tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.jsonl"
    write_checkpoint(chain_factory, path=path)
    _write_entries(chain_factory, 1)
    write_checkpoint(chain_factory, path=path)

    comparison = compare_against_checkpoint(chain_factory, path=path)

    assert comparison.ok is True
    assert comparison.checkpoint_sequence == 4


def test_an_empty_store_never_verifies_against_a_checkpoint(factory, tmp_path: Path) -> None:
    """Anchoring nothing must not read as a pass, whatever the checkpoint file contains."""

    path = tmp_path / "checkpoints.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sequence": 1,
                "head_hash": "sha256:" + "a" * 64,
                "recorded_at": "2026-10-24T00:00:00Z",
                "entries_checked": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    comparison = compare_against_checkpoint(factory, path=path)

    assert comparison.ok is False
    assert comparison.reason == REASON_NO_CHECKPOINT
    assert comparison.stored_head_hash is None


def test_record_rejects_a_non_integer_sequence() -> None:
    with pytest.raises(CheckpointError):
        CheckpointRecord.from_dict(
            {
                "schema_version": "1.0",
                "sequence": "three",
                "head_hash": "sha256:" + "a" * 64,
                "recorded_at": "2026-10-24T00:00:00Z",
                "entries_checked": 3,
            }
        )


def test_record_rejects_a_boolean_sequence() -> None:
    """``bool`` is an ``int`` in Python; a checkpoint must not accept ``true`` as sequence 1."""

    with pytest.raises(CheckpointError):
        CheckpointRecord.from_dict(
            {
                "schema_version": "1.0",
                "sequence": True,
                "head_hash": "sha256:" + "a" * 64,
                "recorded_at": "2026-10-24T00:00:00Z",
                "entries_checked": 3,
            }
        )
