"""Finding query endpoints (SPEC §9.1).

Findings are read-only here. Challenging a finding — rebuttals, verification, dispositions —
belongs to the Phase 3 dispute protocol, not to this module.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request

from nyx.api.deps import get_finding_service
from nyx.api.errors import ApiError
from nyx.domain.enums import FindingCategory, FindingStatus, Severity
from nyx.error_codes import ErrorCode
from nyx.findings.models import Finding
from nyx.findings.service import DEFAULT_PAGE_LIMIT, FindingPage, FindingQuery, FindingService

router = APIRouter(prefix="/findings", tags=["findings"])


@router.get("", response_model=FindingPage)
def list_findings(
    system_id: str | None = Query(default=None, description="Filter by affected system id."),
    status: FindingStatus | None = Query(default=None, description="Filter by finding status."),
    severity: Severity | None = Query(default=None, description="Filter by severity."),
    category: FindingCategory | None = Query(default=None, description="Filter by category."),
    created_after: datetime | None = Query(
        default=None, description="Only findings created at or after this UTC instant."
    ),
    created_before: datetime | None = Query(
        default=None, description="Only findings created at or before this UTC instant."
    ),
    cursor: str | None = Query(default=None, description="Opaque cursor from a previous page."),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=200),
    service: FindingService = Depends(get_finding_service),
) -> FindingPage:
    """List findings, filtered and cursor-paginated with deterministic ordering."""

    return service.list_findings(
        FindingQuery(
            system_id=system_id,
            status=status,
            severity=severity,
            category=category,
            created_after=created_after,
            created_before=created_before,
            cursor=cursor,
            limit=limit,
        )
    )


@router.get("/{finding_id}", response_model=Finding)
def get_finding(
    finding_id: str,
    request: Request,
    service: FindingService = Depends(get_finding_service),
) -> Finding:
    """Retrieve one finding, including its evidence links and limitations.

    The finding's own fields are returned; restricted evidence payloads are not inlined here
    merely because the caller can read the finding (SPEC §9.2).
    """

    finding = service.get_finding(finding_id)
    if finding is None:
        raise ApiError(ErrorCode.NOT_FOUND, message="Finding not found.")
    return finding
