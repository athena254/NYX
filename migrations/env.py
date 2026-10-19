"""Alembic environment wired to NYX settings and ``Base.metadata``."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from nyx.config import Settings
from nyx.storage.db import create_engine_from_settings
from nyx.storage.tables import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = Settings()
# Escape percent signs because ConfigParser interpolates them.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live database connection."""

    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database."""

    connectable = create_engine_from_settings(settings)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
