"""Repository for critic runs (SPEC §5.6)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.findings.models import CriticRun
from nyx.storage.repositories._mapping import as_utc
from nyx.storage.tables import CriticRunRow


class CriticRunRepository:
    """Persistence operations for ``CriticRun``. Never commits implicitly."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: CriticRun) -> CriticRun:
        row = CriticRunRow(
            id=run.id,
            critic_id=run.critic_id,
            critic_type=run.critic_type.value,
            critic_version=run.critic_version,
            rule_version=run.rule_version,
            model_version=run.model_version,
            input_refs=list(run.input_refs),
            input_hashes=list(run.input_hashes),
            started_at=run.started_at,
            completed_at=run.completed_at,
            result=run.result.value,
            rationale=run.rationale,
            tool_run_refs=list(run.tool_run_refs),
            resource_usage=run.resource_usage,
            failure_details=run.failure_details,
            finding_id=run.finding_id,
        )
        self._session.add(row)
        return run

    def link_finding(self, run_id: str, finding_id: str) -> None:
        """Set the ``finding_id`` back-reference once the finding is persisted."""

        row = self._session.get(CriticRunRow, run_id)
        if row is not None:
            row.finding_id = finding_id

    def get(self, run_id: str) -> CriticRun | None:
        row = self._session.get(CriticRunRow, run_id)
        return None if row is None else _to_domain(row)

    def list(
        self,
        *,
        critic_id: str | None = None,
        result: str | None = None,
        finding_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CriticRun]:
        statement = select(CriticRunRow)
        if critic_id is not None:
            statement = statement.where(CriticRunRow.critic_id == critic_id)
        if result is not None:
            statement = statement.where(CriticRunRow.result == result)
        if finding_id is not None:
            statement = statement.where(CriticRunRow.finding_id == finding_id)
        statement = statement.order_by(CriticRunRow.started_at, CriticRunRow.id)
        statement = statement.limit(limit).offset(offset)
        return [_to_domain(row) for row in self._session.scalars(statement)]


def _to_domain(row: CriticRunRow) -> CriticRun:
    return CriticRun.model_validate(
        {
            "id": row.id,
            "critic_id": row.critic_id,
            "critic_type": row.critic_type,
            "critic_version": row.critic_version,
            "rule_version": row.rule_version,
            "model_version": row.model_version,
            "input_refs": row.input_refs,
            "input_hashes": row.input_hashes,
            "started_at": as_utc(row.started_at),
            "completed_at": as_utc(row.completed_at),
            "result": row.result,
            "rationale": row.rationale,
            "tool_run_refs": row.tool_run_refs,
            "resource_usage": row.resource_usage,
            "failure_details": row.failure_details,
            "finding_id": row.finding_id,
        }
    )
