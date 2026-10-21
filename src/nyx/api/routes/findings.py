"""Finding query endpoints (SPEC §9.1).

Findings are read-only here. Challenging a finding — rebuttals, verification, dispositions —
belongs to the Phase 3 dispute protocol, not to this module.

A finding references evidence by ID. Restricted evidence content is never inlined
into a finding response; when the caller lacks ``EVIDENCE_RESTRICTED`` the response
carries a tombstone naming the omitted evidence and its classification while the
ID itself remains (``docs/privacy-and-retention.md`` §6). Finding access never
implies evidence access.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from nyx.api.deps import get_finding_service, get_session
from nyx.api.errors import ApiError
from nyx.auth.dependency import current_principal
from nyx.auth.principal import AuthenticatedPrincipal
from nyx.domain.enums import FindingCategory, FindingStatus, Sensitivity, Severity
from nyx.error_codes import ErrorCode
from nyx.findings.lifecycle import Capability
from nyx.findings.models import Finding
from nyx.findings.service import DEFAULT_PAGE_LIMIT, FindingPage, FindingQuery, FindingService
from nyx.storage.repositories.evidence import EvidenceRepository

router = APIRouter(prefix="/findings", tags=["findings"])

_TOMBSTONE = (
    "Evidence {evidence_id} is classified 'restricted'; its payload is omitted "
    "because the caller lacks the 'evidence.restricted' capability."
)


def _tombstone_restricted(
    finding: Finding,
    repository: EvidenceRepository,
    *,
    may_read_restricted: bool,
) -> Finding:
    """Return ``finding`` with a tombstone for restricted evidence content it references."""

    if may_read_restricted:
        return finding
    omitted = [
        evidence_id
        for evidence_id in finding.evidence_ids
        if (item := repository.get(evidence_id)) is not None
        and item.sensitivity is Sensitivity.RESTRICTED
    ]
    if not omitted:
        return finding
    notes = [_TOMBSTONE.format(evidence_id=evidence_id) for evidence_id in omitted]
    return finding.model_copy(update={"limitations": [*finding.limitations, *notes][:32]})


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
    principal: AuthenticatedPrincipal = Depends(current_principal),
    session: Session = Depends(get_session),
    service: FindingService = Depends(get_finding_service),
) -> FindingPage:
    """List findings, filtered and cursor-paginated with deterministic ordering."""

    page = service.list_findings(
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
    may_read_restricted = Capability.EVIDENCE_RESTRICTED in principal.capabilities
    if may_read_restricted or not page.items:
        return page
    repository = EvidenceRepository(session)
    return page.model_copy(
        update={
            "items": [
                _tombstone_restricted(finding, repository, may_read_restricted=may_read_restricted)
                for finding in page.items
            ]
        }
    )


@router.get("/{finding_id}", response_model=Finding)
def get_finding(
    finding_id: str,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(current_principal),
    session: Session = Depends(get_session),
    service: FindingService = Depends(get_finding_service),
) -> Finding:
    """Retrieve one finding, including its evidence links and limitations.

    The finding's own fields are returned; restricted evidence payloads are not inlined here
    merely because the caller can read the finding (SPEC §9.2).
    """

    del request
    finding = service.get_finding(finding_id)
    if finding is None:
        raise ApiError(ErrorCode.NOT_FOUND, message="Finding not found.")
    return _tombstone_restricted(
        finding,
        EvidenceRepository(session),
        may_read_restricted=Capability.EVIDENCE_RESTRICTED in principal.capabilities,
    )
