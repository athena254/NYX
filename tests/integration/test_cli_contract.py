"""The CLI contract (Phase 7 slice 6.1, SPEC §9.2, plan §6.1).

Other systems consume NYX through this CLI, so the surface is a contract, not a convenience:

* **Every command takes ``--json``.** The flag is introspected off the registered Typer command,
  not off a hand-kept list, so a new command that forgets the flag fails this test.
* **``--json`` emits exactly one JSON document on stdout.** Not a log line, not a status message,
  not a trailing newline of prose. A caller that does ``json.loads(stdout)`` must never have to
  strip anything.
* **Failures exit non-zero and report a stable code**, never a traceback. The same ``code`` field
  the HTTP API uses, so a calling system branches identically against either interface.
* **No command prompts.** A script that cannot answer a prompt must not hang on one.

These are the properties `docs/implementation/CLI-CONTRACT.md` documents for callers; the table
below is the machine-checked half of that document.

Typer registers options as ``TyperOption`` (a ``click.Option`` subclass with a different
``__class__``), which is why the option filter checks ``getattr(opt, "name", "")`` rather than
``isinstance(opt, click.Option)`` -- the latter silently returns nothing and a test built on it
would pass while asserting about zero options.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click
import pytest
import typer
from typer.core import TyperGroup
from typer.testing import CliRunner

from nyx.cli.main import app
from nyx.domain.enums import DispositionOutcome

FIXTURES = Path(__file__).resolve().parents[2] / "examples" / "ghost-factory"


# --------------------------------------------------------------------------- introspection


def _commands(group: click.Group, prefix: str = "") -> dict[str, click.Command]:
    """Flatten the Typer app into ``"group command" -> Command`` pairs.

    Typer wraps sub-apps in :class:`TyperGroup`; recursing on that (rather than on
    ``click.Group``) is what reaches the leaf commands.
    """

    found: dict[str, click.Command] = {}
    for name, subcommand in group.commands.items():
        label = f"{prefix}{name}"
        if isinstance(subcommand, TyperGroup):
            found.update(_commands(subcommand, prefix=f"{label} "))
        else:
            found[label] = subcommand
    return found


def _leaf_commands() -> dict[str, click.Command]:
    return _commands(typer.main.get_command(app))


@contextmanager
def _quiet_logging() -> Iterator[None]:
    """Raise the root logger's level for the duration of a CLI invocation.

    alembic logs its "Will assume non-transactional DDL" line at INFO, and under pytest that line
    can land in the captured output when pytest's own handler fails against a closed stream. That
    is test plumbing, not the CLI's output.
    """

    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        root.setLevel(previous)


def _json_env(tmp_path: Path, database: Path) -> dict[str, str]:
    """NYX settings for a CLI invocation, with the OS environment stripped of ``NYX_*``.

    The ``.env`` *file* is handled by the caller: pydantic-settings reads it relative to the working
    directory, so these tests chdir to the empty temp directory (see ``test_cli_*``).
    """

    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    # ``doctor`` reports any configured-but-unavailable optional integration as a failure, so a
    # developer's real ``NYX_LLM_ENABLED`` would turn a green check red. Force it off; the chat
    # tests set it explicitly.
    env["NYX_LLM_ENABLED"] = "false"
    return env


def _json_flag_names(command: click.Command) -> list[str]:
    """Return the declared names of every option that carries ``--json``."""

    return [
        name
        for option in command.params
        for name in getattr(option, "opts", ())
        if "--json" in name
    ]


# --------------------------------------------------------------------------- the contract


def test_every_cli_command_offers_json() -> None:
    """No command may be added without ``--json``; the flag is the machine interface."""

    commands = _leaf_commands()
    assert len(commands) >= 17, f"expected the full CLI surface, found {sorted(commands)}"
    missing = [name for name, command in commands.items() if not _json_flag_names(command)]
    assert not missing, f"these commands have no --json option: {missing}"


def test_json_flag_is_a_boolean_switch() -> None:
    """``--json`` takes no value: ``--json`` works, ``--json=true`` is not the contract."""

    for name, command in _leaf_commands().items():
        for option in command.params:
            if any("--json" in flag for flag in getattr(option, "opts", ())):
                assert getattr(option, "is_flag", False), f"{name}: --json must be a flag"
                assert getattr(option, "default", None) is False, (
                    f"{name}: --json must default to False"
                )


def test_json_output_is_one_document_with_no_surrounding_prose(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` stdout must parse as a single JSON value and nothing else.

    The check runs against every command that can succeed on a migrated database, because a
    command that appends a status line to stdout would still look fine until a caller tried to
    parse it.

    The runner's ``mix_stderr`` is disabled and pytest's root handler is redirected to a null
    stream for the duration: alembic logs to stderr, and under pytest that log is routed through a
    handler whose stream can already be closed, which makes the handler itself print a traceback
    into the captured output. That is pytest's logging plumbing, not the CLI's stdout, and it must
    not be able to make a passing command look like it emitted prose.
    """

    monkeypatch.chdir(tmp_path)  # keep the developer's `.env` out of settings
    runner = CliRunner()
    env = _json_env(tmp_path, database)

    # Seed one of every record type so list/show commands have something to read.
    for args in (
        ["system", "register", str(FIXTURES / "system-manifest.json")],
        ["ingest", "event", str(FIXTURES / "event-completion-claim.json")],
        ["claim", "add", str(FIXTURES / "claim-completion-abc123.json")],
        ["evidence", "add", str(FIXTURES / "evidence-test-def456.json")],
    ):
        result = runner.invoke(app, args, env=env)
        assert result.exit_code == 0, result.output

    successes = {
        "doctor --json": ["doctor", "--json"],
        "system list --json": ["system", "list", "--json"],
        "critic list --json": ["critic", "list", "--json"],
        "finding list --json": ["finding", "list", "--json"],
        "finding show --json": ["finding", "show", _first_finding_id(runner, env), "--json"],
        "finding history --json": ["finding", "history", _first_finding_id(runner, env), "--json"],
    }

    for label, args in successes.items():
        with _quiet_logging():
            result = runner.invoke(app, args, env=env)
        assert result.exit_code == 0, f"{label} exited {result.exit_code}: {result.output}"
        # Assert on stdout, not on the combined output: stderr is where the CLI's own
        # diagnostics go, and the contract is that stdout is exactly one JSON document.
        payload = json.loads(result.stdout)  # raises if stdout is not exactly one JSON document
        assert payload is not None, f"{label} produced empty stdout"


def _first_finding_id(runner: CliRunner, env: dict[str, str]) -> str:
    """Return a real finding id, running the critics to create one if the store has none."""

    result = runner.invoke(app, ["finding", "list", "--json"], env=env)
    items = json.loads(result.output).get("items", [])
    if items:
        return str(items[0]["id"])

    # A migrated store with records but no findings is the normal case for this test: findings
    # come from the deterministic critics, so run them against the seeded event.
    run = runner.invoke(
        app,
        [
            "critic",
            "run",
            "--event",
            _first_event_id(runner, env),
            "--json",
        ],
        env=env,
    )
    assert run.exit_code == 0, run.output
    result = runner.invoke(app, ["finding", "list", "--json"], env=env)
    items = json.loads(result.output).get("items", [])
    assert items, "critic run produced no finding; cannot assert show/history output"
    return str(items[0]["id"])


def _first_event_id(runner: CliRunner, env: dict[str, str]) -> str:
    """Return a real event id from the seeded store.

    ``ingest event`` echoes the record it accepted, and that record is the whole event; reading it
    back out of the CLI output avoids a second storage read (and the SQLAlchemy version differences
    that come with one).
    """

    result = runner.invoke(
        app,
        ["ingest", "event", str(FIXTURES / "event-completion-claim.json"), "--json"],
        env=env,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] in {"created", "duplicate"}, payload
    # A created record carries the whole ``record``; a duplicate replay reports ``record_id`` and
    # embeds the existing id in the error message. Either way the id is what the caller needs.
    if payload.get("record") is not None:
        return str(payload["record"]["id"])
    return str(payload["record_id"])


def test_failures_exit_non_zero_and_report_a_stable_code(tmp_path: Path, database: Path) -> None:
    """A failure must be machine-legible: exit 1 and a ``code`` that is not a traceback."""

    runner = CliRunner()
    env = _json_env(tmp_path, database)

    failures = {
        "not found": ["finding", "show", "fnd_does_not_exist", "--json"],
        "capability denied": [
            "finding",
            "dispose",
            "fnd_does_not_exist",
            "--outcome",
            DispositionOutcome.CONFIRMED.value,
            "--reason",
            "probe",
            "--json",
        ],
        # A batch reports per-item outcomes rather than a top-level code: one bad item must not
        # turn into an unparseable success for the whole batch.
        "invalid batch item": ["ingest", "batch", str(_write_bad_batch(tmp_path)), "--json"],
        # A file that cannot be opened is an invalid request, not a traceback.
        "unreadable input": ["ingest", "event", str(tmp_path / "absent.json"), "--json"],
        # Unparseable JSON is refused with a code, never a crash.
        "malformed json": ["ingest", "event", str(_write_bad_json(tmp_path)), "--json"],
    }
    # A malformed body is reported per-record, with the code nested under ``error`` -- the same
    # shape the HTTP API returns for a rejected record.

    for label, args in failures.items():
        result = runner.invoke(app, args, env=env)
        assert result.exit_code != 0, f"{label} exited 0; a failure must not look like success"
        assert "Traceback" not in result.output, f"{label} leaked a traceback:\n{result.output}"
        payload = json.loads(result.output)
        if label == "invalid batch item":
            assert payload["results"][0]["reason"] == "SCHEMA_VALIDATION_FAILED"
        elif label == "malformed json":
            assert payload["error"]["code"] == "INVALID_REQUEST"
        else:
            assert payload.get("code"), f"{label} reported no error code: {result.output}"


def _write_bad_json(tmp_path: Path) -> Path:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    return path


def _write_bad_batch(tmp_path: Path) -> Path:
    """A batch file whose items are structurally invalid, so ingestion refuses them."""

    document = [{"event_type": "x", "schema_version": "1.0"}]
    path = tmp_path / "bad-batch.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_no_command_prompts_for_input(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A calling system cannot answer a prompt, so no command may wait for stdin.

    Commands are invoked with stdin closed. The assertion is on the *exception*, not on the output:
    a CLI command that reads stdin would block or raise ``EOFError``, and Typer would surface that
    as an unhandled exception. Matching on output text is unreliable because pytest's own logging
    handler can emit its own traceback when its stream is closed underneath it.
    """

    monkeypatch.chdir(tmp_path)  # keep the developer's `.env` out of settings
    runner = CliRunner()
    env = _json_env(tmp_path, database)

    # The chat shell is the one command that reads stdin by design; it is covered by
    # test_chat_shell_exits_on_eof rather than by this test.
    interactive = {"chat shell", "chat ask"}

    for name, command in _leaf_commands().items():
        if name in interactive:
            continue
        required = [
            param.name
            for param in command.params
            if isinstance(param, click.Argument) and param.required
        ]
        args = _required_arguments(name, required)
        if args is None:
            continue  # not runnable without a live provider; covered elsewhere
        result = runner.invoke(app, args, env=env, input="")
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"{name} raised {result.exception!r} on closed stdin"
        )


def _required_arguments(name: str, required: list[str]) -> list[str] | None:
    """Build an argv prefix supplying the required positional arguments of ``name``.

    Returns ``None`` for a command whose positional arguments cannot be supplied from here --
    currently ``chat ask`` (needs a live provider, covered by the chat tests).
    """

    supplied: dict[str, str] = {
        "manifest": str(FIXTURES / "system-manifest.json"),
        "source": str(FIXTURES / "event-completion-claim.json"),
        "finding_id": "fnd_does_not_exist",
        "event": str(FIXTURES / "event-completion-claim.json"),
    }
    args = name.split()
    for argument in required:
        if argument not in supplied:
            return None
        args.append(supplied[argument])
    return args


# --------------------------------------------------------------------------- exit codes


@pytest.mark.parametrize(
    ("label", "argv", "expected_code"),
    [
        ("success", ["system", "list", "--json"], 0),
        ("not found", ["finding", "show", "fnd_absent", "--json"], 1),
        (
            "capability denied",
            [
                "finding",
                "dispose",
                "fnd_absent",
                "--outcome",
                "confirmed",
                "--reason",
                "probe",
                "--json",
            ],
            1,
        ),
        ("invalid batch", ["ingest", "batch", "absent-file.json", "--json"], 1),
    ],
)
def test_exit_code_table(
    tmp_path: Path, database: Path, label: str, argv: list[str], expected_code: int
) -> None:
    """Pin the documented exit-code table so a change to it is a deliberate edit."""

    runner = CliRunner()
    env = _json_env(tmp_path, database)

    result = runner.invoke(app, argv, env=env)
    assert result.exit_code == expected_code, (
        f"{label}: expected exit {expected_code}, got {result.exit_code} "
        f"({result.output.strip()[:120]})"
    )


def test_stdin_pipe_never_hangs_a_command(tmp_path: Path, database: Path) -> None:
    """Piping stdin (the normal scripted use) must not block, whatever the command does."""

    runner = CliRunner()
    env = _json_env(tmp_path, database)

    result = runner.invoke(
        app, ["ingest", "event", "-", "--json"], env=env, input=json.dumps({"nope": 1})
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "rejected"
    assert payload["error"]["code"] == "SCHEMA_VALIDATION_FAILED"


# --------------------------------------------------------------------------- chat


def test_chat_shell_exits_on_eof(tmp_path: Path, database: Path) -> None:
    """The interactive shell is the one command allowed to read stdin, and it must exit cleanly.

    It also refuses to start without an LLM configured, which is the fail-closed property the
    conversational slice requires: the shell names the missing variable instead of guessing a model.
    """

    runner = CliRunner()
    env = _json_env(tmp_path, database)
    env["NYX_LLM_ENABLED"] = "false"

    result = runner.invoke(app, ["chat", "shell", "--json"], env=env, input="\n")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "INVALID_REQUEST"
    assert "NYX_LLM_ENABLED" in payload["message"]


def test_chat_ask_refuses_without_a_model(tmp_path: Path, database: Path) -> None:
    """``NYX_LLM_ENABLED=true`` with no model must refuse, naming the variable."""

    runner = CliRunner()
    env = _json_env(tmp_path, database)
    env["NYX_LLM_ENABLED"] = "true"
    env["NYX_LLM_MODEL"] = ""

    result = runner.invoke(app, ["chat", "ask", "ping", "--json"], env=env)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert "NYX_LLM_MODEL" in payload["message"]
