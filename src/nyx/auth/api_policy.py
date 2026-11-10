"""Explicit, default-deny operation capabilities for the authenticated API group.

Authentication establishes identity; it does not grant ingestion, critic execution or verdict
powers. New protected endpoints are denied until they have an entry in this reviewable map.
"""

from typing import Annotated

from fastapi import Depends, Request

from nyx.api.errors import ApiError
from nyx.auth.dependency import current_principal
from nyx.auth.principal import AuthenticatedPrincipal
from nyx.error_codes import ErrorCode
from nyx.findings.lifecycle import Capability

# Keys are framework-resolved Python endpoints, never a caller-supplied identity or URL string.
_DISPOSITION_AUTHORITIES = frozenset(
    {Capability.DISPOSITION, Capability.ACCEPT_RISK, Capability.ADMINISTER}
)
_DENIAL_CODES = {
    frozenset({Capability.EVIDENCE_READ}): ErrorCode.EVIDENCE_ACCESS_DENIED,
    _DISPOSITION_AUTHORITIES: ErrorCode.CAPABILITY_REQUIRED,
}

OPERATION_CAPABILITIES: dict[tuple[str, str], Capability | frozenset[Capability]] = {
    ("nyx.api.routes.systems.create_system", "POST"): Capability.SYSTEMS_WRITE,
    ("nyx.api.routes.systems.list_systems", "GET"): Capability.SYSTEMS_READ,
    ("nyx.api.routes.events.ingest_event", "POST"): Capability.EVENTS_WRITE,
    ("nyx.api.routes.events.ingest_event_batch", "POST"): Capability.EVENTS_WRITE,
    ("nyx.api.routes.events.get_event", "GET"): Capability.EVENTS_READ,
    ("nyx.api.routes.claims.register_claim", "POST"): Capability.CLAIMS_WRITE,
    ("nyx.api.routes.claims.get_claim", "GET"): Capability.CLAIMS_READ,
    ("nyx.api.routes.evidence.register_evidence", "POST"): Capability.EVIDENCE_WRITE,
    ("nyx.api.routes.evidence.get_evidence", "GET"): Capability.EVIDENCE_READ,
    ("nyx.api.routes.findings.list_findings", "GET"): Capability.FINDINGS_READ,
    ("nyx.api.routes.findings.get_finding", "GET"): Capability.FINDINGS_READ,
    ("nyx.api.routes.critics.get_critics", "GET"): Capability.CRITICS_READ,
    ("nyx.api.routes.critics.run_critics", "POST"): Capability.CRITICS_RUN,
    ("nyx.api.routes.disputes.submit_rebuttal", "POST"): Capability.FINDINGS_REBUT,
    ("nyx.api.routes.disputes.list_rebuttals", "GET"): Capability.FINDINGS_READ,
    ("nyx.api.routes.disputes.request_verification", "POST"): Capability.VERIFICATION_REQUEST,
    ("nyx.api.routes.disputes.run_pending_verification", "POST"): Capability.VERIFICATION_RUN,
    ("nyx.api.routes.disputes.list_verification_jobs", "GET"): Capability.VERIFICATION_READ,
    ("nyx.api.routes.disputes.get_verification_job", "GET"): Capability.VERIFICATION_READ,
    # Admit the authority family; the service preflights the exact requested outcome
    # against the lifecycle's canonical capability before resolving any references.
    ("nyx.api.routes.disputes.record_disposition", "POST"): _DISPOSITION_AUTHORITIES,
    ("nyx.api.routes.disputes.get_history", "GET"): Capability.FINDINGS_READ,
    ("nyx.api.routes.disputes.list_policy_versions", "GET"): Capability.POLICIES_READ,
}


def require_api_access(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(current_principal)],
) -> AuthenticatedPrincipal:
    """Authorize the exact routed operation before its handler or storage lookups run."""
    endpoint = request.scope.get("endpoint")
    identity = f"{getattr(endpoint, '__module__', '')}.{getattr(endpoint, '__name__', '')}"
    required = OPERATION_CAPABILITIES.get((identity, request.method.upper()))
    if required is None:
        raise ApiError(
            ErrorCode.FORBIDDEN, message="This API operation has no configured capability."
        )
    permitted = frozenset({required}) if isinstance(required, Capability) else required
    if permitted.isdisjoint(principal.capabilities):
        code = _DENIAL_CODES.get(permitted, ErrorCode.FORBIDDEN)
        names = ", ".join(sorted(capability.value for capability in permitted))
        raise ApiError(
            code, message=f"One of these capabilities is required for this operation: {names}."
        )
    return principal
