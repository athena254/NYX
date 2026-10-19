"""Engine, session factory, and transaction boundaries for NYX storage."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from nyx.config import Settings
from nyx.storage.tables import Base

SessionFactory = sessionmaker[Session]


def ensure_sqlite_parent_directory(database_url: str) -> None:
    """Create the parent directory for a file-backed SQLite URL, if needed."""

    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return
    database = url.database
    if database is None or database in {"", ":memory:"}:
        return
    path = Path(database)
    parent = path.parent
    if str(parent) not in {"", "."}:
        parent.mkdir(parents=True, exist_ok=True)


def create_engine_from_settings(settings: Settings | None = None) -> Engine:
    """Create a SQLAlchemy engine from the effective settings."""

    effective = settings or Settings()
    ensure_sqlite_parent_directory(effective.database_url)
    connect_args: dict[str, object] = {}
    if make_url(effective.database_url).drivername.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(effective.database_url, connect_args=connect_args)


def create_session_factory(engine: Engine) -> SessionFactory:
    """Return a session factory bound to ``engine``."""

    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: SessionFactory) -> Iterator[Session]:
    """Yield a session and own its commit/rollback boundary.

    The transaction commits when the block exits normally and rolls back when it raises.
    Repositories never commit; callers own the transaction boundary.
    """

    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine) -> None:
    """Create every table directly; intended for tests and local bootstrap."""

    Base.metadata.create_all(engine)


def drop_all(engine: Engine) -> None:
    """Drop every table; intended only for isolated tests."""

    Base.metadata.drop_all(engine)
