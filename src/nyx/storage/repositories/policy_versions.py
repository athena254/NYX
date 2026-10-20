"""Repository for recorded policy versions (acceptance scenario 22).

Append-only. Registering a new version inserts a row; the previous version stays readable
forever, so a reader can always establish which policy was in force when a disposition was
decided.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.disputes.models import PolicyVersion
from nyx.storage.repositories._mapping import as_utc
from nyx.storage.tables import PolicyVersionRow


class PolicyVersionRepository:
    """Persistence operations for ``PolicyVersion``. Never commits implicitly."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, version: PolicyVersion) -> PolicyVersion:
        self._session.add(
            PolicyVersionRow(
                id=version.id,
                rule_id=version.rule_id,
                version=version.version,
                digest=version.digest,
                changed_by=version.changed_by,
                changed_at=version.changed_at,
                change_reason=version.change_reason,
            )
        )
        return version

    def get(self, policy_version_id: str) -> PolicyVersion | None:
        row = self._session.get(PolicyVersionRow, policy_version_id)
        return None if row is None else _to_domain(row)

    def list(
        self, *, rule_id: str | None = None, changed_by: str | None = None, limit: int = 100
    ) -> list[PolicyVersion]:
        statement = select(PolicyVersionRow)
        if rule_id is not None:
            statement = statement.where(PolicyVersionRow.rule_id == rule_id)
        if changed_by is not None:
            statement = statement.where(PolicyVersionRow.changed_by == changed_by)
        statement = statement.order_by(PolicyVersionRow.changed_at, PolicyVersionRow.id).limit(
            limit
        )
        return [_to_domain(row) for row in self._session.scalars(statement)]

    def latest(self, rule_id: str) -> PolicyVersion | None:
        """Return the newest recorded version of ``rule_id``."""

        statement = (
            select(PolicyVersionRow)
            .where(PolicyVersionRow.rule_id == rule_id)
            .order_by(PolicyVersionRow.changed_at.desc(), PolicyVersionRow.id.desc())
            .limit(1)
        )
        row = self._session.scalars(statement).first()
        return None if row is None else _to_domain(row)


def _to_domain(row: PolicyVersionRow) -> PolicyVersion:
    return PolicyVersion.model_validate(
        {
            "id": row.id,
            "rule_id": row.rule_id,
            "version": row.version,
            "digest": row.digest,
            "changed_by": row.changed_by,
            "changed_at": as_utc(row.changed_at),
            "change_reason": row.change_reason,
        }
    )
