"""FastAPI dependency providers backed by application state."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from nyx.auth.checker import ConfigBackedCapabilityChecker
from nyx.auth.dependency import current_principal, get_capability_store
from nyx.auth.principal import AuthenticatedPrincipal
from nyx.config import Settings
from nyx.disputes.service import DisputeService
from nyx.disputes.verification import VerificationAllowlist, default_allowlist
from nyx.findings.lifecycle import CapabilityChecker
from nyx.findings.service import FindingService
from nyx.ingestion.service import IngestionService
from nyx.storage.db import session_scope


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


def get_session(request: Request) -> Iterator[Session]:
    """Yield one transactional session per request.

    The route owns the transaction boundary: the block commits on success and rolls back on any
    exception, matching ``session_scope``. Dispute routes use this because a rebuttal, a
    verification job, and a disposition each need exactly one transaction.
    """

    factory = request.app.state.session_factory
    with session_scope(factory) as session:
        yield session


def get_dispute_service(
    request: Request, session: Session = Depends(get_session)
) -> DisputeService:
    """Return a :class:`DisputeService` bound to this request's session and application state."""

    return DisputeService(
        session,
        capability_checker=get_capability_checker(request),
        allowlist=get_verification_allowlist(request),
    )


def get_principal(request: Request) -> AuthenticatedPrincipal:
    """Return the authenticated principal for this request."""

    return current_principal(request)


def get_capability_checker(request: Request) -> CapabilityChecker:
    """Return the capability checker bound to the running application.

    The application may not have bound a checker yet. When a checker is bound, use
    it — including the explicit deny-all default the application factory installs
    until the orchestrator wires the authenticated one. When none is bound,
    construct the real configuration-backed checker rather than denying by default.
    """

    checker = getattr(request.app.state, "capability_checker", None)
    if checker is not None:
        return checker  # type: ignore[no-any-return]
    return ConfigBackedCapabilityChecker(get_capability_store(request))


def get_verification_allowlist(request: Request) -> VerificationAllowlist:
    """Return the verification allowlist bound to the running application."""

    allowlist = getattr(request.app.state, "verification_allowlist", None)
    return allowlist or default_allowlist()
