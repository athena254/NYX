"""Repository for findings (SPEC §5.5)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from nyx.findings.models import Finding
from nyx.storage.repositories._mapping import as_json, as_utc
from nyx.storage.repositories.critic_runs import CriticRunRepository
from nyx.storage.tables import FindingRow


class FindingRepository:
    """Persistence operations for ``Finding``. Never commits implicitly."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, finding: Finding) -> Finding:
        row = FindingRow(
            id=finding.id,
            schema_version=finding.schema_version,
            title=finding.title,
            summary=finding.summary,
            category=finding.category.value,
            severity=finding.severity.value,
            confidence=(None if finding.confidence is None else as_json(finding.confidence)),
            scope=as_json(finding.scope),
            scope_system_id=finding.scope.system_id,
            affected_system_ids=list(finding.affected_system_ids),
            claim_ids=list(finding.claim_ids),
            event_ids=list(finding.event_ids),
            evidence_ids=list(finding.evidence_ids),
            supporting_evidence_ids=list(finding.supporting_evidence_ids),
            counterevidence_ids=list(finding.counterevidence_ids),
            reasoning=as_json(finding.reasoning),
            limitations=list(finding.limitations),
            unknowns=list(finding.unknowns),
            recommended_action=as_json(finding.recommended_action),
            status=finding.status.value,
            dedupe_key=finding.dedupe_key,
            dedupe_rationale=finding.dedupe_rationale,
            duplicate_of=finding.duplicate_of,
            created_at=finding.created_at,
            updated_at=finding.updated_at,
        )
        self._session.add(row)
        return finding

    def get(self, finding_id: str) -> Finding | None:
        row = self._session.get(FindingRow, finding_id)
        return None if row is None else _to_domain(self._session, row)

    def find_by_dedupe_key(self, dedupe_key: str) -> Finding | None:
        """Return the first non-duplicate finding recorded under ``dedupe_key``."""

        statement = (
            select(FindingRow)
            .where(FindingRow.dedupe_key == dedupe_key)
            .order_by(FindingRow.created_at, FindingRow.id)
            .limit(1)
        )
        row = self._session.scalars(statement).first()
        return None if row is None else _to_domain(self._session, row)

    def list_after(
        self,
        *,
        after: tuple[datetime, str] | None,
        limit: int,
        system_id: str | None = None,
        status: str | None = None,
        severity: str | None = None,
        category: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> list[Finding]:
        """Return a cursor page ordered by ``(created_at, id)`` with optional filters."""

        statement = select(FindingRow)
        if system_id is not None:
            statement = statement.where(FindingRow.scope_system_id == system_id)
        if status is not None:
            statement = statement.where(FindingRow.status == status)
        if severity is not None:
            statement = statement.where(FindingRow.severity == severity)
        if category is not None:
            statement = statement.where(FindingRow.category == category)
        if created_after is not None:
            statement = statement.where(FindingRow.created_at >= created_after)
        if created_before is not None:
            statement = statement.where(FindingRow.created_at <= created_before)
        if after is not None:
            statement = statement.where(tuple_(FindingRow.created_at, FindingRow.id) > after)
        statement = statement.order_by(FindingRow.created_at, FindingRow.id).limit(limit)
        return [_to_domain(self._session, row) for row in self._session.scalars(statement)]


def _to_domain(session: Session, row: FindingRow) -> Finding:
    """Rebuild a ``Finding`` from its row, including the critic runs that produced it.

    ``Finding.critic_runs`` is a required, non-empty list: a finding is composed *from* a
    critic run, so a finding with no linked runs is a self-consistency violation rather than
    an empty-but-valid case. Populating it here is therefore not decoration -- hardcoding an
    empty list made every read-back fail validation, which broke both
    ``GET /api/v1/findings`` and ``GET /api/v1/findings/{id}``.
    """

    runs = CriticRunRepository(session).list(finding_id=row.id)
    return Finding.model_validate(
        {
            "id": row.id,
            "schema_version": row.schema_version,
            "title": row.title,
            "summary": row.summary,
            "category": row.category,
            "severity": row.severity,
            "confidence": row.confidence,
            "scope": row.scope,
            "affected_system_ids": row.affected_system_ids,
            "claim_ids": row.claim_ids,
            "event_ids": row.event_ids,
            "evidence_ids": row.evidence_ids,
            "supporting_evidence_ids": row.supporting_evidence_ids,
            "counterevidence_ids": row.counterevidence_ids,
            "reasoning": row.reasoning,
            "limitations": row.limitations,
            "unknowns": row.unknowns,
            "recommended_action": row.recommended_action,
            "critic_runs": [run.model_dump() for run in runs],
            "status": row.status,
            "created_at": as_utc(row.created_at),
            "updated_at": as_utc(row.updated_at),
            "dedupe_key": row.dedupe_key,
            "dedupe_rationale": row.dedupe_rationale,
            "duplicate_of": row.duplicate_of,
        }
    )
