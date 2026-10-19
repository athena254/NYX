"""Finding query service and the transaction boundary for critic runs.

The service owns ``session_scope`` boundaries exactly like the ingestion service: repositories
never commit, and each public method is atomic. Critic execution, run persistence, and finding
composition all happen inside one transaction, so a critic that fails mid-run cannot leave a
half-written finding.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from nyx.critics.registry import CriticRunOutcome, run_selected
from nyx.domain.base import Identifier, NyxModel, UtcDatetime
from nyx.domain.enums import FindingCategory, FindingStatus, Severity
from nyx.domain.pagination import clamp_limit, decode_cursor, encode_cursor
from nyx.findings.models import Finding
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.findings import FindingRepository

DEFAULT_PAGE_LIMIT = 50


class FindingQuery(NyxModel):
    """Filter criteria for ``GET /api/v1/findings`` (SPEC §9.1)."""

    system_id: Identifier | None = None
    status: FindingStatus | None = None
    severity: Severity | None = None
    category: FindingCategory | None = None
    created_after: UtcDatetime | None = None
    created_before: UtcDatetime | None = None
    cursor: str | None = None
    limit: int = Field(default=DEFAULT_PAGE_LIMIT, ge=1, le=200)


class FindingPage(NyxModel):
    """One cursor page of findings."""

    items: list[Finding]
    next_cursor: str | None = None


class FindingService:
    """Query findings and run critics, owning transaction boundaries."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def get_finding(self, finding_id: str) -> Finding | None:
        with session_scope(self._session_factory) as session:
            return FindingRepository(session).get(finding_id)

    def list_findings(self, query: FindingQuery) -> FindingPage:
        limit = clamp_limit(query.limit)
        after: tuple[datetime, str] | None = None
        if query.cursor is not None:
            after = decode_cursor(query.cursor)

        with session_scope(self._session_factory) as session:
            rows = FindingRepository(session).list_after(
                after=after,
                limit=limit + 1,
                system_id=query.system_id,
                status=None if query.status is None else query.status.value,
                severity=None if query.severity is None else query.severity.value,
                category=None if query.category is None else query.category.value,
                created_after=query.created_after,
                created_before=query.created_before,
            )

        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
        return FindingPage(items=page, next_cursor=next_cursor)

    def run_critics(
        self,
        event_id: str,
        critic_ids: list[str] | None = None,
    ) -> list[CriticRunOutcome]:
        """Run the selected critics against one event and persist runs and findings."""

        with session_scope(self._session_factory) as session:
            return run_selected(session, event_id=event_id, critic_ids=critic_ids)
