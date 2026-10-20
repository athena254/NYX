"""Dispute endpoints: rebuttals, verification jobs, dispositions, and history.

These endpoints are the HTTP face of :class:`~nyx.disputes.service.DisputeService`, which owns the
workflow. The route layer adds nothing to the logic: it validates the request body, opens one
transaction, and maps failures onto the shared error envelope.

Two things are deliberately NOT here yet, both Phase 4:

* **Authentication.** The actor is taken from the request body rather than an authenticated
  identity, so every dispute record is currently self-asserted. Phase 4 replaces this with a
  verified principal.
* **Authorisation.** Capability checking is enforced by the lifecycle machine, but the checker bound
  to the application is deny-all until Phase 4 wires real authorisation. A default deployment
  therefore accepts rebuttals and refuses dispositions, which is the safe direction.

The append-only contract is visible in the surface itself: there is no ``PATCH`` or ``DELETE`` here,
and no endpoint that rewrites a prior record.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from nyx.api.deps import get_dispute_service
from nyx.api.errors import ApiError, request_id_for
from nyx.disputes.models import Disposition, Rebuttal, VerificationJob
from nyx.disputes.service import (
    DisputeService,
    FindingNotFoundError,
    UnknownEvidenceError,
)
from nyx.disputes.verification import VerificationNotPermittedError
from nyx.domain.base import Identifier, NyxModel, PrincipalRef
from nyx.domain.enums import DispositionOutcome, PrincipalKind, RebuttalRequestedOutcome
from nyx.error_codes import ErrorCode
from nyx.findings.lifecycle import CapabilityDeniedError, InvalidLifecycleTransitionError
from nyx.findings.models import Finding
from nyx.storage.db import session_scope

router = APIRouter(tags=["disputes"])


# --------------------------------------------------------------------------- request bodies


class RebuttalRequest(NyxModel):
    """Body for ``POST /api/v1/findings/{finding_id}/rebuttals``."""

    submitted_by: Identifier
    argument: str
    evidence_ids: list[Identifier] = []
    requested_outcome: RebuttalRequestedOutcome = RebuttalRequestedOutcome.NO_CHANGE


class VerificationRequest(NyxModel):
    """Body for ``POST /api/v1/findings/{finding_id}/verification-jobs``."""

    check_id: str
    requested_by: Identifier


class DispositionRequest(NyxModel):
    """Body for ``POST /api/v1/findings/{finding_id}/dispositions``."""

    outcome: DispositionOutcome
    rationale: str
    decided_by: Identifier
    policy_version: str
    verification_run_ids: list[Identifier] = []
    counterevidence_ids: list[Identifier] = []


# --------------------------------------------------------------------------- responses


class RebuttalListResponse(NyxModel):
    """Every rebuttal recorded against a finding, oldest first."""

    items: list[Rebuttal]


class VerificationJobResponse(NyxModel):
    """One verification attempt, including its independence statement."""

    job: VerificationJob
    request_id: str


class VerificationJobListResponse(NyxModel):
    """Verification attempts for a finding, oldest first."""

    items: list[VerificationJob]


class PolicyVersionModel(NyxModel):
    """One recorded policy change: the version, who changed it, and why."""

    id: Identifier
    rule_id: Identifier
    version: str
    digest: str
    changed_by: Identifier
    change_reason: str


class PolicyVersionListResponse(NyxModel):
    """Recorded policy changes, newest first."""

    items: list[PolicyVersionModel]


class DisputeHistoryResponse(NyxModel):
    """The full dispute record for one finding: the finding and everything appended to it."""

    finding: Finding
    rebuttals: list[Rebuttal]
    verification_jobs: list[VerificationJob]
    dispositions: list[Disposition]
    current_disposition: Disposition | None = None


# --------------------------------------------------------------------------- helpers


def _as_actor(identifier: Identifier) -> PrincipalRef:
    """Build the actor recorded on a lifecycle transition.

    Self-asserted for now, and the audit trail says so. Phase 4 replaces this with the authenticated
    principal; until then every dispute record carries the identity the caller claimed.
    """

    return PrincipalRef(id=identifier, kind=PrincipalKind.PRINCIPAL)


def _require_finding(service: DisputeService, finding_id: str) -> Finding:
    try:
        return service.get_history(finding_id=finding_id).finding
    except FindingNotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, message=exc.detail) from exc


# --------------------------------------------------------------------------- rebuttals


@router.post("/findings/{finding_id}/rebuttals", response_model=Rebuttal)
def submit_rebuttal(
    finding_id: str,
    payload: RebuttalRequest,
    service: DisputeService = Depends(get_dispute_service),
) -> Rebuttal:
    """Record a challenge to a finding.

    The rebuttal appends to history; the original finding is not overwritten. Its status advances
    along the canonical path to ``rebuttal_pending``.
    """

    try:
        return service.submit_rebuttal(
            finding_id=finding_id,
            actor=_as_actor(payload.submitted_by),
            submitted_by=payload.submitted_by,
            argument=payload.argument,
            evidence_ids=payload.evidence_ids,
            requested_outcome=payload.requested_outcome,
        )
    except UnknownEvidenceError as exc:
        raise ApiError(ErrorCode.UNKNOWN_EVIDENCE_ID, message=exc.detail) from exc
    except FindingNotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, message=exc.detail) from exc


@router.get("/findings/{finding_id}/rebuttals", response_model=RebuttalListResponse)
def list_rebuttals(
    finding_id: str,
    service: DisputeService = Depends(get_dispute_service),
) -> RebuttalListResponse:
    """List every rebuttal recorded against a finding, oldest first."""

    _require_finding(service, finding_id)
    return RebuttalListResponse(items=service.get_history(finding_id=finding_id).rebuttals)


# --------------------------------------------------------------------------- verification


@router.post("/findings/{finding_id}/verification-jobs", response_model=VerificationJobResponse)
def request_verification(
    finding_id: str,
    payload: VerificationRequest,
    request: Request,
    service: DisputeService = Depends(get_dispute_service),
) -> VerificationJobResponse:
    """Queue an allowlisted verification check for a finding.

    A check that is not on the allowlist is refused here rather than queued, so untrusted input
    never reaches a command line.
    """

    try:
        job = service.request_verification(
            finding_id=finding_id,
            check_id=payload.check_id,
            requested_by=payload.requested_by,
        )
    except VerificationNotPermittedError as exc:
        raise ApiError(ErrorCode.VERIFICATION_NOT_PERMITTED, message=exc.detail) from exc
    except FindingNotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, message=exc.detail) from exc
    return VerificationJobResponse(job=job, request_id=request_id_for(request))


@router.post(
    "/findings/{finding_id}/verification-jobs/run",
    response_model=VerificationJobListResponse,
)
def run_pending_verification(
    finding_id: str,
    service: DisputeService = Depends(get_dispute_service),
) -> VerificationJobListResponse:
    """Run the finding's queued verification jobs through the allowlist.

    A job that is refused or fails is recorded as such, never rewritten into a success. The
    finding is not moved: only an authorised disposition does that.
    """

    _require_finding(service, finding_id)
    jobs = service.run_pending_verification(finding_id=finding_id)
    return VerificationJobListResponse(items=jobs)


@router.get("/findings/{finding_id}/verification-jobs", response_model=VerificationJobListResponse)
def list_verification_jobs(
    finding_id: str,
    service: DisputeService = Depends(get_dispute_service),
) -> VerificationJobListResponse:
    """List every verification attempt recorded for a finding, oldest first."""

    _require_finding(service, finding_id)
    return VerificationJobListResponse(
        items=service.get_history(finding_id=finding_id).verification_jobs
    )


@router.get("/verification-jobs/{job_id}", response_model=VerificationJobResponse)
def get_verification_job(
    job_id: str,
    request: Request,
    service: DisputeService = Depends(get_dispute_service),
) -> VerificationJobResponse:
    """Retrieve one verification attempt, including its independence statement."""

    job = service.get_verification_job(job_id=job_id)
    if job is None:
        raise ApiError(ErrorCode.NOT_FOUND, message="Verification job not found.")
    return VerificationJobResponse(job=job, request_id=request_id_for(request))


# --------------------------------------------------------------------------- dispositions


@router.post("/findings/{finding_id}/dispositions", response_model=Disposition)
def record_disposition(
    finding_id: str,
    payload: DispositionRequest,
    service: DisputeService = Depends(get_dispute_service),
) -> Disposition:
    """Record an authorised outcome and move the finding to the matching state.

    The new disposition supersedes the previous one by linking to it; the previous record is left
    exactly as it was. A principal without the disposition capability is refused.
    """

    try:
        return service.record_disposition(
            finding_id=finding_id,
            actor=_as_actor(payload.decided_by),
            outcome=payload.outcome,
            rationale=payload.rationale,
            decided_by=payload.decided_by,
            policy_version=payload.policy_version,
            verification_run_ids=payload.verification_run_ids,
            counterevidence_ids=payload.counterevidence_ids,
        )
    except CapabilityDeniedError as exc:
        raise ApiError(ErrorCode.CAPABILITY_REQUIRED, message=exc.detail) from exc
    except InvalidLifecycleTransitionError as exc:
        raise ApiError(ErrorCode.INVALID_LIFECYCLE_TRANSITION, message=exc.detail) from exc
    except FindingNotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, message=exc.detail) from exc


# --------------------------------------------------------------------------- history


@router.get("/findings/{finding_id}/history", response_model=DisputeHistoryResponse)
def get_history(
    finding_id: str,
    service: DisputeService = Depends(get_dispute_service),
) -> DisputeHistoryResponse:
    """Return the full dispute record for a finding, including the disposition chain.

    Nothing is omitted: the original finding, every rebuttal, every verification attempt, and every
    disposition are all present, because none of them is ever deleted.
    """

    _require_finding(service, finding_id)
    history = service.get_history(finding_id=finding_id)
    return DisputeHistoryResponse(
        finding=history.finding,
        rebuttals=history.rebuttals,
        verification_jobs=history.verification_jobs,
        dispositions=history.dispositions,
        current_disposition=history.current_disposition,
    )


# --------------------------------------------------------------------------- policies


@router.get("/policies/versions", response_model=PolicyVersionListResponse)
def list_policy_versions(
    request: Request,
    rule_id: str | None = Query(default=None, description="Only changes to this rule id."),
) -> PolicyVersionListResponse:
    """List recorded policy changes, newest first (acceptance scenario 22).

    Each entry records the version, who changed it, and why, so a reader can establish which policy
    was in force when a disposition was decided.
    """

    from nyx.policies.versions import PolicyVersionStore

    with session_scope(request.app.state.session_factory) as session:
        versions = PolicyVersionStore(session).list(rule_id=rule_id)
        return PolicyVersionListResponse(
            items=[
                PolicyVersionModel(
                    id=item.id,
                    rule_id=item.rule_id,
                    version=item.version,
                    digest=item.digest,
                    changed_by=item.changed_by,
                    change_reason=item.change_reason,
                )
                for item in versions
            ]
        )
