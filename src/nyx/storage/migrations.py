"""Shared migration-state inspection used by readiness and ``nyx doctor``.

Readiness must say "not ready" when the database is behind the migration head. This module
holds the single implementation; the API readiness probe and the CLI both call it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine


def repository_root() -> Path:
    """Return the repository root inferred from this module's location.

    In a frozen (PyInstaller) build the repository data (alembic.ini, migrations/,
    docs/openapi.json) is shipped inside the bundle's extraction dir, so prefer that
    when it actually contains the expected files.
    """
    import os
    import sys

    candidates: list[Path] = []
    env_root = os.environ.get("NYX_REPO_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    frozen = getattr(sys, "frozen", False)
    if frozen:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass))
    candidates.append(Path(__file__).resolve().parents[3])
    for candidate in candidates:
        if (candidate / "alembic.ini").exists():
            return candidate
    return candidates[-1]



@dataclass(frozen=True)
class MigrationState:
    """The result of comparing the database revision with the script head."""

    at_head: bool
    current: tuple[str, ...]
    head: tuple[str, ...]
    error: str | None = None

    def summary(self) -> str:
        """Return a human-readable, secret-free summary."""

        if self.error is not None:
            return self.error
        if self.at_head:
            return "at head"
        current = list(self.current) or ["base"]
        return f"database is not at head (current={current}, head={list(self.head)})"


def _alembic_config(database_url: str) -> Config | None:
    root = repository_root()
    ini_path = root / "alembic.ini"
    if not ini_path.exists():
        return None
    config = Config(str(ini_path))
    config.set_main_option("script_location", str(root / "migrations"))
    # Escape percent signs because ConfigParser interpolates them.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def inspect_migrations(database_url: str, engine: Engine) -> MigrationState:
    """Inspect the migration state without raising or leaking connection details."""

    config = _alembic_config(database_url)
    if config is None:
        return MigrationState(at_head=False, current=(), head=(), error="alembic.ini not found")
    try:
        script = ScriptDirectory.from_config(config)
        expected = tuple(sorted(script.get_heads()))
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            current = tuple(sorted(context.get_current_heads()))
    except Exception as exc:  # noqa: BLE001 - report only a safe type name
        return MigrationState(
            at_head=False,
            current=(),
            head=(),
            error=f"could not inspect revisions ({type(exc).__name__})",
        )
    if not expected:
        return MigrationState(
            at_head=False, current=current, head=(), error="no migration head found"
        )
    return MigrationState(at_head=current == expected, current=current, head=expected)
