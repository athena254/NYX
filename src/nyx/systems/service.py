"""System registry service.

Registration is idempotent on ``name`` + ``environment``: the first registration creates the
record; later registrations return the existing record and append an audit entry noting the
replay. Every registration attempt writes an audit entry inside the same transaction.

The caller owns the transaction boundary. Use ``nyx.storage.db.session_scope`` when a service
call should commit atomically.
"""

from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from nyx.audit.writer import AuditEntryInput, AuditWriter
from nyx.domain.base import PrincipalRef
from nyx.domain.enums import AuditOutcome, PrincipalKind, SystemStatus
from nyx.domain.systems import MonitoredSystem
from nyx.storage.repositories.systems import SystemRepository

_REGISTRY_ACTOR = PrincipalRef(
    id="nyx.system-registry",
    kind=PrincipalKind.SERVICE,
    display_name="NYX system registry",
)


def register_system(
    session: Session,
    system: MonitoredSystem,
    *,
    actor: PrincipalRef | None = None,
    request_id: str | None = None,
) -> MonitoredSystem:
    """Register a system idempotently and append an audit entry.

    Returns the stored record. If a system with the same name and environment already exists,
    that record is returned unchanged and no duplicate row is created.
    """

    repository = SystemRepository(session)
    existing = repository.find_by_name_environment(system.name, system.environment)
    effective_actor = actor or _REGISTRY_ACTOR
    effective_request_id = request_id or f"req_{secrets.token_hex(12)}"

    if existing is not None:
        _audit(
            session,
            actor=effective_actor,
            request_id=effective_request_id,
            action="system.registration_replayed",
            target_id=existing.id,
            reason=(
                f"system {existing.name!r} in environment {existing.environment.value!r} "
                "was already registered; returning the existing record"
            ),
        )
        return existing

    repository.add(system)
    _audit(
        session,
        actor=effective_actor,
        request_id=effective_request_id,
        action="system.registered",
        target_id=system.id,
        reason=(
            f"registered system {system.name!r} in environment "
            f"{system.environment.value!r} with kind {system.kind.value!r}"
        ),
    )
    return system


def get_system(session: Session, system_id: str) -> MonitoredSystem | None:
    """Return a system by NYX identifier, or ``None`` when absent."""

    return SystemRepository(session).get(system_id)


def list_systems(
    session: Session,
    *,
    status: SystemStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[MonitoredSystem]:
    """Return systems in stable creation order."""

    return SystemRepository(session).list(status=status, limit=limit, offset=offset)


def _audit(
    session: Session,
    *,
    actor: PrincipalRef,
    request_id: str,
    action: str,
    target_id: str,
    reason: str,
) -> None:
    AuditWriter(session).append(
        AuditEntryInput(
            actor=actor,
            action=action,
            target_type="monitored_system",
            target_id=target_id,
            request_id=request_id,
            outcome=AuditOutcome.SUCCESS,
            reason=reason,
        )
    )
