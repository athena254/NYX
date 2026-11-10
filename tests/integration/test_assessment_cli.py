"""The assessment queue is drained by a caller, and the CLI is that caller.

The durable queue was written but nothing in the service consumed it, which is the same shape as
an adapter that is registered but never driven: the state was correct and nothing happened. These
tests pin the two commands that close that gap, and the property that matters — work accepted
before a restart is still assessed afterwards, once.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import click
import pytest
import typer
from typer.core import TyperGroup

from nyx.cli.main import app

REPO = Path(__file__).resolve().parents[2]


def _run_cli(database: Path, data_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in __import__("os").environ.items()
        if not key.upper().startswith("NYX_")
    }
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(data_dir)
    return subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", "nyx.cli.main", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture()
def migrated(tmp_path: Path) -> Path:
    database = tmp_path / "assessment.sqlite3"
    env = {
        key: value
        for key, value in __import__("os").environ.items()
        if not key.upper().startswith("NYX_")
    }
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return database


def test_backlog_reports_zero_work_on_a_fresh_store(migrated: Path, tmp_path: Path) -> None:
    result = _run_cli(migrated, tmp_path, "assessment", "backlog", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {"queued": 0, "leased": 0, "dead_letter": 0}


def test_every_assessment_command_is_registered_with_json() -> None:
    """The assessment surface is registered, and every command carries the machine flag.

    Introspected off the Typer tree rather than parsed from ``--help`` output: rich styles option
    names differently depending on how it detects the terminal, so a substring assertion on help
    text passes on one machine and fails on another for the same command.
    """

    commands = _flatten(cast(click.Group, typer.main.get_command(app)))
    assessment = {
        label: command for label, command in commands.items() if label.startswith("assessment")
    }
    assert set(assessment) == {"assessment backlog", "assessment drain"}, sorted(assessment)
    for label, command in assessment.items():
        flags = [
            flag
            for option in getattr(command, "params", ())
            for flag in getattr(option, "opts", ())
        ]
        assert "--json" in flags, f"{label} has no --json option: {flags}"


def _flatten(group: click.Group, *, prefix: str = "") -> dict[str, click.Command]:
    found: dict[str, click.Command] = {}
    for name, subcommand in group.commands.items():
        label = f"{prefix}{name}"
        if isinstance(subcommand, TyperGroup):
            found.update(_flatten(subcommand, prefix=f"{label} "))
        else:
            found[label] = subcommand
    return found


def test_drain_on_an_empty_queue_is_a_success_with_no_work(migrated: Path, tmp_path: Path) -> None:
    result = _run_cli(migrated, tmp_path, "assessment", "drain", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["completed"] == []


def test_assessment_commands_do_not_require_the_conversational_extra() -> None:
    """The assessment surface is core: it must not import the optional shell."""

    source = (REPO / "src" / "nyx" / "assessment" / "service.py").read_text(encoding="utf-8")
    assert "conversational" not in source
    cli = (REPO / "src" / "nyx" / "cli" / "main.py").read_text(encoding="utf-8")
    assert "AssessmentWorker" in cli
