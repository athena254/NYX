"""Health and readiness endpoints (SPEC §9.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import Engine

from nyx import __version__
from nyx.api.deps import get_engine, get_settings
from nyx.api.errors import ApiError
from nyx.api.schemas import HealthResponse, ReadyResponse
from nyx.config import Settings
from nyx.error_codes import ErrorCode
from nyx.storage.migrations import inspect_migrations

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness: the process is up and serving requests."""

    return HealthResponse(status="ok", service="nyx", version=__version__)


@router.get("/ready", response_model=ReadyResponse)
def ready(
    request: Request,
    engine: Engine = Depends(get_engine),
    settings: Settings = Depends(get_settings),
) -> ReadyResponse:
    """Readiness: the database is reachable and migrations are at head.

    Returns ``503`` with a structured, secret-free error when not ready.
    """

    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - never leak connection details
        raise ApiError(
            ErrorCode.STORAGE_UNAVAILABLE,
            message="The database is not reachable.",
        ) from exc
    state = inspect_migrations(settings.database_url, engine)
    if state.error is not None or not state.at_head:
        raise ApiError(
            ErrorCode.DATABASE_NOT_READY,
            message=f"Database is not ready: {state.summary()}.",
        )
    return ReadyResponse(status="ready", database="ok", migrations="at head")
