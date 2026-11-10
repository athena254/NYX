"""v0.0.2 checkpoint comparison must validate bodies, not stored hash labels."""

from pathlib import Path

from sqlalchemy import text

from nyx.audit.checkpoint import compare_against_checkpoint, write_checkpoint
from nyx.audit.writer import AuditEntryInput, AuditWriter
from nyx.domain.base import PrincipalRef
from nyx.domain.enums import PrincipalKind
from nyx.storage.db import SessionFactory, session_scope


def _append(factory: SessionFactory, count: int = 3) -> None:
    with session_scope(factory) as session:
        writer = AuditWriter(session)
        for index in range(count):
            writer.append(
                AuditEntryInput(
                    actor=PrincipalRef(id="audit-owner", kind=PrincipalKind.HUMAN),
                    action="system.registered",
                    target_type="system",
                    target_id=f"sys_composite_{index}",
                    request_id=f"composite-{index}",
                    reason=f"original inspectable reason {index}",
                )
            )


def test_body_only_tamper_fails_external_checkpoint(
    session_factory: SessionFactory, tmp_path: Path
) -> None:
    _append(session_factory)
    anchor = tmp_path / "anchor.jsonl"
    write_checkpoint(session_factory, path=anchor)
    with session_scope(session_factory) as session:
        session.execute(
            text("UPDATE audit_entries SET reason=:reason WHERE sequence=1"),
            {"reason": "altered body with original hash retained"},
        )
    result = compare_against_checkpoint(session_factory, path=anchor)
    assert not result.ok, "A matching stored hash is not integrity verification of its body"
    assert result.reason != "checkpoint_verified"


def test_interior_deletion_fails_checkpoint(
    session_factory: SessionFactory, tmp_path: Path
) -> None:
    _append(session_factory)
    anchor = tmp_path / "anchor.jsonl"
    write_checkpoint(session_factory, path=anchor)
    with session_scope(session_factory) as session:
        session.execute(text("DELETE FROM audit_entries WHERE sequence=2"))
    assert not compare_against_checkpoint(session_factory, path=anchor).ok


def test_clean_append_after_historical_anchor_passes(
    session_factory: SessionFactory, tmp_path: Path
) -> None:
    _append(session_factory)
    anchor = tmp_path / "anchor.jsonl"
    record = write_checkpoint(session_factory, path=anchor)
    _append(session_factory, count=1)
    result = compare_against_checkpoint(session_factory, path=anchor)
    assert result.ok
    assert result.checkpoint_sequence == record.sequence
    assert result.entries_checked == 4


def test_composite_comparison_reads_one_ordered_snapshot(
    session_factory: SessionFactory, tmp_path: Path
) -> None:
    from sqlalchemy import event

    _append(session_factory)
    anchor = tmp_path / "anchor.jsonl"
    write_checkpoint(session_factory, path=anchor)
    statements: list[str] = []
    engine = session_factory.kw["bind"]

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith("select") and "audit_entries" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = compare_against_checkpoint(session_factory, path=anchor)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result.ok
    assert len(statements) == 1, "Integrity and anchor must use the same SELECT snapshot"


def test_actual_cli_against_rejects_body_tampering(
    session_factory: SessionFactory, tmp_path: Path, monkeypatch
) -> None:
    from typer.testing import CliRunner

    from nyx.cli.main import app

    _append(session_factory)
    anchor = tmp_path / "anchor.jsonl"
    write_checkpoint(session_factory, path=anchor)
    with session_scope(session_factory) as session:
        session.execute(
            text("UPDATE audit_entries SET reason=:reason WHERE sequence=1"),
            {"reason": "changed body, not a recomputed rewrite"},
        )
    monkeypatch.setenv("NYX_DATABASE_URL", str(session_factory.kw["bind"].url))
    monkeypatch.setenv("NYX_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(app, ["audit", "verify", "--against", str(anchor), "--json"])
    assert result.exit_code != 0
    assert "audit_chain_invalid" in result.output
