"""Minimal NYX command-line interface.

Phase 1a exposes only ``nyx doctor``. The full CLI surface (system, ingest, claim, evidence,
critic, finding, and audit commands) belongs to Phase 1b and later phases.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import typer
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from nyx.config import Settings
from nyx.storage.db import create_engine_from_settings

app = typer.Typer(
    name="nyx",
    help="NYX evidence-backed oversight service.",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """NYX evidence-backed oversight service."""


@app.command()
def doctor() -> None:
    """Check configuration, database, migrations, and writable paths.

    Prints only non-sensitive facts. Secret values and database passwords are never shown.
    Exits non-zero if any check fails.
    """

    failures = 0

    try:
        settings = Settings()
    except ValidationError as exc:
        typer.echo("[fail] configuration: invalid settings")
        for error in exc.errors(include_input=False):
            location = ".".join(str(part) for part in error.get("loc", ()))
            typer.echo(f"       {location or '<root>'}: {error.get('msg', 'invalid value')}")
        raise typer.Exit(code=1) from None
    except Exception as exc:  # noqa: BLE001 - report a safe type name, never the raw message
        typer.echo(f"[fail] configuration: could not load settings ({type(exc).__name__})")
        raise typer.Exit(code=1) from None

    backend = make_url(settings.database_url).get_backend_name()
    typer.echo(f"[ok] configuration: host={settings.host} port={settings.port} database={backend}")
    typer.echo(
        f"     data_dir={settings.data_dir} redaction={settings.redaction_enabled} "
        f"auth_mode={settings.auth_mode}"
    )

    if _check_data_dir(settings.data_dir):
        typer.echo("[ok] data directory: writable")
    else:
        typer.echo(f"[fail] data directory: not writable ({settings.data_dir})")
        failures += 1

    engine = None
    try:
        engine = create_engine_from_settings(settings)
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        typer.echo("[ok] database: reachable")
    except Exception as exc:  # noqa: BLE001 - no raw connection details or URLs
        typer.echo(f"[fail] database: not reachable ({type(exc).__name__})")
        failures += 1

    if engine is not None:
        migration_failure = _check_migrations(settings, engine)
        if migration_failure is None:
            typer.echo("[ok] migrations: at head")
        else:
            typer.echo(f"[fail] migrations: {migration_failure}")
            failures += 1
        engine.dispose()

    integrations = []
    if settings.llm_enabled:
        integrations.append("llm")
    integrations.extend(settings.enabled_adapters)
    if integrations:
        typer.echo(
            f"[fail] optional integrations: configured but unavailable ({', '.join(integrations)})"
        )
        failures += 1
    else:
        typer.echo("[ok] optional integrations: none enabled")

    if failures:
        typer.echo(f"doctor: {failures} check(s) failed")
        raise typer.Exit(code=1)
    typer.echo("doctor: all checks passed")


def _check_data_dir(data_dir: Path) -> bool:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        descriptor, path = tempfile.mkstemp(prefix=".nyx-doctor-", dir=str(data_dir))
        os.close(descriptor)
        Path(path).unlink()
    except OSError:
        return False
    return True


def _check_migrations(settings: Settings, engine: Engine) -> str | None:
    root = Path(__file__).resolve().parents[3]
    ini_path = root / "alembic.ini"
    if not ini_path.exists():
        return "alembic.ini not found"
    config = Config(str(ini_path))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    try:
        script = ScriptDirectory.from_config(config)
        expected = set(script.get_heads())
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            current = set(context.get_current_heads())
    except Exception as exc:  # noqa: BLE001 - safe failure summary
        return f"could not inspect revisions ({type(exc).__name__})"
    if not expected:
        return "no migration head found"
    if current != expected:
        current_label = sorted(current) or ["base"]
        return f"database is not at head (current={current_label}, head={sorted(expected)})"
    return None


if __name__ == "__main__":
    app()
