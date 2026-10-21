"""Evidence registration endpoint (SPEC §9.1) with capability gating (SPEC §12).

Reading evidence is gated on capabilities independent of finding access
(``docs/privacy-and-retention.md`` §6). ``access_policy_ref`` values in the reserved
``evidence:`` namespace are strict: an unrecognised name is refused at registration
rather than silently mapped to the least-privilege read capability. Free-form
policy references outside that namespace keep their legacy meaning and require
``EVIDENCE_READ``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response

from nyx.api.deps import get_service
from nyx.api.errors import ApiError, api_error_from, request_id_for
from nyx.auth.dependency import require_capability
from nyx.auth.principal import AuthenticatedPrincipal
from nyx.domain.enums import Sensitivity
from nyx.domain.evidence import EvidenceItem
from nyx.error_codes import ErrorCode
from nyx.findings.lifecycle import Capability
from nyx.ingestion.errors import FieldDetail
from nyx.ingestion.inputs import EvidenceInput
from nyx.ingestion.service import IngestionService

router = APIRouter(prefix="/evidence", tags=["evidence"])

_RESTRICTED_POLICY_REF = "evidence:restricted"
_READ_POLICY_REFS = frozenset(
    {"evidence:read", "evidence:internal", "evidence:public", "evidence:standard"}
)
_EVIDENCE_NAMESPACE = "evidence:"


def required_capability_for_access_policy(access_policy_ref: str) -> Capability:
    """Map an access policy reference to the capability needed to read the item.

    ``evidence:restricted`` requires ``EVIDENCE_RESTRICTED``. Known
    ``evidence:*`` read policies and legacy free-form references require
    ``EVIDENCE_READ``. An unknown value inside the reserved namespace raises
    ``ValueError`` so registration fails closed instead of downgrading it.
    """

    if access_policy_ref == _RESTRICTED_POLICY_REF:
        return Capability.EVIDENCE_RESTRICTED
    if access_policy_ref in _READ_POLICY_REFS:
        return Capability.EVIDENCE_READ
    if access_policy_ref.startswith(_EVIDENCE_NAMESPACE):
        raise ValueError(f"unknown access_policy_ref '{access_policy_ref}'")
    return Capability.EVIDENCE_READ


@router.post("", response_model=EvidenceItem, status_code=201)
def register_evidence(
    evidence: EvidenceInput,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthenticatedPrincipal = Depends(require_capability(Capability.EVIDENCE_READ)),
    service: IngestionService = Depends(get_service),
) -> EvidenceItem:
    """Register an evidence item or reference."""

    del response, principal
    try:
        required_capability_for_access_policy(evidence.access_policy_ref)
    except ValueError as exc:
        raise ApiError(
            ErrorCode.INVALID_REQUEST,
            message=str(exc),
            field_details=[
                FieldDetail(
                    field="/access_policy_ref",
                    issue=str(exc),
                    code="unknown_access_policy_ref",
                )
            ],
        ) from exc

    result = service.register_evidence(
        evidence, request_id=request_id_for(request), idempotency_key=idempotency_key
    )
    if result.status != "created":
        assert result.error is not None  # nosec B101 - invariant for non-created results
        raise api_error_from(result.error, request)
    assert isinstance(result.record, EvidenceItem)  # nosec B101 - set by the gateway
    return result.record


@router.get("/{evidence_id}", response_model=EvidenceItem)
def get_evidence(
    evidence_id: str,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(require_capability(Capability.EVIDENCE_READ)),
    service: IngestionService = Depends(get_service),
) -> EvidenceItem:
    """Retrieve one evidence item by its stable id.

    A ``restricted`` item — by classification or by its access policy — additionally
    requires ``EVIDENCE_RESTRICTED``. The refusal carries no evidence content.
    """

    del request
    evidence = service.get_evidence(evidence_id)
    if evidence is None:
        raise ApiError(ErrorCode.NOT_FOUND, message="Evidence not found.")

    restricted = (
        evidence.sensitivity is Sensitivity.RESTRICTED
        or required_capability_for_access_policy(evidence.access_policy_ref)
        is Capability.EVIDENCE_RESTRICTED
    )
    if restricted and Capability.EVIDENCE_RESTRICTED not in principal.capabilities:
        raise ApiError(
            ErrorCode.EVIDENCE_ACCESS_DENIED,
            message="The caller lacks the capability required to read this evidence.",
        )
    return evidence
