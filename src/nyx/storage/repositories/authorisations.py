"""Repository for authorisation records. Append-only: no update or delete is exposed."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.domain.authorisation import Authorisation
from nyx.domain.enums import RiskAction
from nyx.storage.repositories._mapping import as_json, as_utc
from nyx.storage.tables import AuthorisationRow


class AuthorisationRepository:
    """Persistence operations for :class:`~nyx.domain.authorisation.Authorisation`.

    The read that matters is :meth:`find_covering`: given an action, does a *valid* authorisation
    exist? "Valid" means approver differs from actor, the scope covers the action's scope, and it
    has not expired. The check is here rather than in the critic so that C-006 and any future
    authorisation-gated probe agree on one definition of "covered".
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, authorisation: Authorisation) -> Authorisation:
        self._session.add(_to_row(authorisation))
        return authorisation

    def get(self, authorisation_id: str) -> Authorisation | None:
        row = self._session.get(AuthorisationRow, authorisation_id)
        return None if row is None else _to_domain(row)

    def list(
        self,
        *,
        actor_id: str | None = None,
        approver_id: str | None = None,
        action: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Authorisation]:
        statement = select(AuthorisationRow)
        if actor_id is not None:
            statement = statement.where(AuthorisationRow.actor_id == actor_id)
        if approver_id is not None:
            statement = statement.where(AuthorisationRow.approver_id == approver_id)
        if action is not None:
            statement = statement.where(AuthorisationRow.action == action)
        statement = (
            statement.order_by(AuthorisationRow.approved_at, AuthorisationRow.id)
            .limit(limit)
            .offset(offset)
        )
        return [_to_domain(row) for row in self._session.scalars(statement)]

    def find_covering(
        self,
        *,
        actor_id: str,
        action: str,
        risk_action: RiskAction,
        action_scope: str,
        at: datetime | None = None,
    ) -> Authorisation | None:
        """Return the most recent valid authorisation covering this action, if any.

        "Valid" means the approver differs from the actor (enforced at construction), the scope
        covers the action's scope, and it had not expired at ``at``. ``at=None`` means now.
        """

        statement = (
            select(AuthorisationRow)
            .where(AuthorisationRow.actor_id == actor_id)
            .where(AuthorisationRow.action == action)
            .where(AuthorisationRow.risk_action == risk_action.value)
            .order_by(AuthorisationRow.approved_at.desc(), AuthorisationRow.id)
            .limit(50)
        )
        for row in self._session.scalars(statement):
            domain = _to_domain(row)
            if domain.is_expired(at):
                continue
            if not domain.scope_matches(action_scope):
                continue
            return domain
        return None


def _to_row(authorisation: Authorisation) -> AuthorisationRow:
    return AuthorisationRow(
        id=authorisation.id,
        schema_version=authorisation.schema_version,
        action=authorisation.action,
        risk_action=authorisation.risk_action,
        actor=as_json(authorisation.actor),
        actor_id=authorisation.actor.id,
        approver=as_json(authorisation.approver),
        approver_id=authorisation.approver.id,
        environment=authorisation.environment,
        scope_ref=authorisation.scope_ref,
        approved_at=authorisation.approved_at,
        expires_at=authorisation.expires_at,
        subject_system_id=authorisation.subject_system_id,
        note=authorisation.note,
        granted_by=None if authorisation.granted_by is None else as_json(authorisation.granted_by),
    )


def _to_domain(row: AuthorisationRow) -> Authorisation:
    return Authorisation.model_validate(
        {
            "id": row.id,
            "schema_version": row.schema_version,
            "action": row.action,
            "risk_action": row.risk_action,
            "actor": row.actor,
            "approver": row.approver,
            "environment": row.environment,
            "scope_ref": row.scope_ref,
            "approved_at": as_utc(row.approved_at),
            "expires_at": None if row.expires_at is None else as_utc(row.expires_at),
            "subject_system_id": row.subject_system_id,
            "note": row.note,
            "granted_by": row.granted_by,
        }
    )
