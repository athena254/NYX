"""FastAPI application factory for the Phase 1 API subset."""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import Engine

import nyx.critics  # noqa: F401 - registers the deterministic critic pack
from nyx import __version__
from nyx.api.errors import register_exception_handlers
from nyx.api.middleware import RequestIdMiddleware
from nyx.api.routes import (
    claims,
    critics,
    disputes,
    events,
    evidence,
    findings,
    health,
    systems,
)
from nyx.config import Settings
from nyx.critics import register_storage_backed
from nyx.disputes.verification import default_allowlist
from nyx.findings.lifecycle import DenyAllCapabilityChecker
from nyx.findings.service import FindingService
from nyx.ingestion.service import IngestionService
from nyx.observability.logging import configure_logging
from nyx.storage.db import (
    SessionFactory,
    create_engine_from_settings,
    create_session_factory,
)

_API_V1 = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    session_factory: SessionFactory | None = None,
) -> FastAPI:
    """Build the NYX FastAPI application.

    An injected ``engine``/``session_factory`` lets tests share a migrated test database.
    When omitted, both are derived from the effective settings; no connection is opened
    until a request needs one.
    """

    effective = settings or Settings()
    configure_logging(effective.log_level)

    if engine is None:
        engine = create_engine_from_settings(effective)
    if session_factory is None:
        session_factory = create_session_factory(engine)

    app = FastAPI(
        title="NYX",
        version=__version__,
        description=(
            "Evidence-backed oversight service. Exposes system registration, "
            "event/claim/evidence ingestion, deterministic critics, findings, and "
            "health/readiness."
        ),
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = effective
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.service = IngestionService(session_factory, effective)
    app.state.finding_service = FindingService(session_factory)
    # Phase 4 replaces both: real authentication supplies the checker, and the allowlist is
    # already explicit (a check is code, registered in one place). Deny-all is the safe default.
    app.state.capability_checker = DenyAllCapabilityChecker()
    app.state.verification_allowlist = default_allowlist()

    # C-010 audits NYX's own store, so it must be handed THIS session factory -- the one every
    # request writes through -- or it would inspect a different database than the live one.
    register_storage_backed(session_factory)

    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)

    app.include_router(systems.router, prefix=_API_V1)
    app.include_router(events.router, prefix=_API_V1)
    app.include_router(claims.router, prefix=_API_V1)
    app.include_router(evidence.router, prefix=_API_V1)
    app.include_router(findings.router, prefix=_API_V1)
    app.include_router(critics.router, prefix=_API_V1)
    app.include_router(disputes.router, prefix=_API_V1)
    app.include_router(health.router, prefix=_API_V1)

    return app
