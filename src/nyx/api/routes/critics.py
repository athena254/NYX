"""Critic listing and execution endpoints (SPEC §9.1).

A critic run is a request to assess one stored event against the deterministic critic pack.
The run itself never mutates the event, the claim, or the evidence: it reads them, records a
``CriticRun`` row, and — only when a critic actually produces a finding — composes a finding
in the same transaction. A ``no_finding`` result means *only* that this critic found nothing
within its declared scope; it is never a statement that the claim is true.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from nyx.api.deps import get_finding_service
from nyx.api.errors import ApiError, request_id_for
from nyx.critics.registry import CriticRegistryError, list_critics
from nyx.domain.base import Identifier, NyxModel
from nyx.domain.enums import CriticResultStatus
from nyx.error_codes import ErrorCode
from nyx.findings.service import FindingService

router = APIRouter(prefix="/critics", tags=["critics"])


class CriticRunRequest(NyxModel):
    """Body for ``POST /api/v1/critics/run``."""

    event_id: Identifier
    critic_ids: list[str] | None = None


class CriticRunResult(NyxModel):
    """The persisted outcome of one critic run, as returned to the caller."""

    critic_id: str
    critic_version: str
    result: CriticResultStatus
    proposition: str
    rationale: str
    finding_id: str | None = None


class CriticRunResponse(NyxModel):
    """The outcome of a critic run request."""

    results: list[CriticRunResult]
    request_id: str


class CriticInfoModel(NyxModel):
    """The public description of one registered critic."""

    critic_id: str
    critic_version: str
    description: str
    can_assess: list[str]
    cannot_assess: list[str]


class CriticListResponse(NyxModel):
    """Every registered critic, with its declared scope."""

    critics: list[CriticInfoModel]


@router.get("", response_model=CriticListResponse)
def get_critics() -> CriticListResponse:
    """List the registered deterministic critics and what each can and cannot assess."""

    return CriticListResponse(
        critics=[
            CriticInfoModel(
                critic_id=info.critic_id,
                critic_version=info.critic_version,
                description=info.description,
                can_assess=info.can_assess,
                cannot_assess=info.cannot_assess,
            )
            for info in list_critics()
        ]
    )


@router.post("/run", response_model=CriticRunResponse)
def run_critics(
    payload: CriticRunRequest,
    request: Request,
    service: FindingService = Depends(get_finding_service),
) -> CriticRunResponse:
    """Run the selected critics against one event.

    Omitting ``critic_ids`` runs the whole registered pack. Unknown critic ids and unknown
    event ids are rejected before any run is persisted.
    """

    try:
        outcomes = service.run_critics(payload.event_id, payload.critic_ids)
    except CriticRegistryError as exc:
        text = str(exc)
        code = ErrorCode.INVALID_REQUEST if "unknown critic" in text else ErrorCode.NOT_FOUND
        raise ApiError(code, message=text) from exc

    return CriticRunResponse(
        results=[
            CriticRunResult(
                critic_id=outcome.critic_id,
                critic_version=outcome.critic_version,
                result=outcome.result,
                proposition=outcome.proposition,
                rationale=outcome.rationale,
                finding_id=outcome.finding_id,
            )
            for outcome in outcomes
        ],
        request_id=request_id_for(request),
    )
