"""FastAPI dependency providers backed by application state."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy import Engine

from nyx.config import Settings
from nyx.findings.service import FindingService
from nyx.ingestion.service import IngestionService


def get_settings(request: Request) -> Settings:
    """Return the effective settings bound to the running application."""

    return request.app.state.settings  # type: ignore[no-any-return]


def get_service(request: Request) -> IngestionService:
    """Return the ingestion service bound to the running application."""

    return request.app.state.service  # type: ignore[no-any-return]


def get_finding_service(request: Request) -> FindingService:
    """Return the finding service bound to the running application."""

    return request.app.state.finding_service  # type: ignore[no-any-return]


def get_engine(request: Request) -> Engine:
    """Return the database engine bound to the running application."""

    return request.app.state.engine  # type: ignore[no-any-return]
