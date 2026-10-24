"""The explicit critic registry (SPEC §8).

There is no magic discovery: a critic is a class with a stable identity that is registered
explicitly via :func:`register`. The registry lists critics with their declared scope so a
reader can interpret a ``no_finding`` result correctly: it means *only* that this critic
found no issue within its declared scope, never that the claim is true or that no other
critic could find something.

:func:`run_selected` executes critics against one event and persists every run as a
``CriticRun`` row (SPEC §5.6): critic version, input references, input content hashes, start
and complete times, the result, the rationale, and redacted failure details. A run that
produces a finding also composes and persists the finding in the same transaction, with the
run's ``finding_id`` back-reference set.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from nyx.critics.base import Critic, CriticResult
from nyx.critics.context import CriticContext, build_context
from nyx.domain.base import utc_now
from nyx.domain.canonical import content_hash
from nyx.domain.claims import Claim
from nyx.domain.enums import CriticResultStatus
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem
from nyx.findings.composer import compose_finding
from nyx.findings.models import CriticRun, Finding
from nyx.ingestion.redaction import redact
from nyx.storage.repositories.claims import ClaimRepository
from nyx.storage.repositories.critic_runs import CriticRunRepository
from nyx.storage.repositories.events import EventRepository
from nyx.storage.repositories.evidence import EvidenceRepository

_REGISTRY: dict[str, Critic] = {}


class CriticRegistryError(Exception):
    """Raised when a critic id is not registered or the target event does not exist."""


@dataclass(frozen=True)
class CriticInfo:
    """The public description of one registered critic."""

    critic_id: str
    critic_version: str
    description: str
    can_assess: list[str]
    cannot_assess: list[str]


@dataclass(frozen=True)
class CriticRunOutcome:
    """The persisted outcome of one critic run."""

    critic_id: str
    critic_version: str
    result: CriticResultStatus
    proposition: str
    rationale: str
    finding_id: str | None = None


@dataclass(frozen=True)
class RerunOutcome:
    """One critic's result when re-run for verification.

    ``run_id`` is the new ``CriticRun`` row. The original run is never touched, so the audit trail
    shows both what the critic concluded when it raised the finding and what it concluded on
    re-examination.
    """

    critic_id: str
    critic_version: str
    result: CriticResultStatus
    rationale: str
    run_id: str


def register(critic: Critic) -> None:
    """Register a critic. Re-registering the same id replaces the previous entry."""

    _REGISTRY[critic.critic_id] = critic


def list_critics() -> list[CriticInfo]:
    """Return the registered critics in stable id order."""

    return [
        CriticInfo(
            critic_id=critic.critic_id,
            critic_version=critic.critic_version,
            description=critic.description,
            can_assess=list(critic.scope.can_assess),
            cannot_assess=list(critic.scope.cannot_assess),
        )
        for critic in sorted(_REGISTRY.values(), key=lambda item: item.critic_id)
    ]


def get(critic_id: str) -> Critic:
    """Return the critic registered under ``critic_id``."""

    try:
        return _REGISTRY[critic_id]
    except KeyError:
        raise CriticRegistryError(
            f"unknown critic {critic_id!r}; registered critics: {sorted(_REGISTRY)}"
        ) from None


def run_selected(
    session: Session,
    *,
    event_id: str,
    critic_ids: list[str] | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> list[CriticRunOutcome]:
    """Run the selected critics against the event ``event_id``.

    Every run is persisted as a ``CriticRun`` row. Findings are composed and persisted in the
    same transaction; if composition fails, the run row is rolled back with it.
    """

    event = EventRepository(session).get(event_id)
    if event is None:
        raise CriticRegistryError(f"unknown event id {event_id!r}") from None

    context = _build_context(session, event, now=clock())
    selected = (
        [get(item) for item in critic_ids]
        if critic_ids
        else [_REGISTRY[key] for key in sorted(_REGISTRY)]
    )

    outcomes: list[CriticRunOutcome] = []
    for critic in selected:
        started_at = clock()
        failure_details: str | None = None
        try:
            result = critic.run(context)
        except Exception as exc:  # noqa: BLE001 - record the failure, redacted, and continue
            result = _error_result(critic, exc, context)
            failure_details = _redact_failure(exc)
        completed_at = clock()

        run = CriticRun(
            critic_id=critic.critic_id,
            critic_type=critic.critic_type,
            critic_version=critic.critic_version,
            input_refs=_input_refs(context),
            input_hashes=_input_hashes(context),
            started_at=started_at,
            completed_at=completed_at,
            result=result.result,
            rationale=result.rationale,
            failure_details=failure_details,
            model_provider=result.model_provider,
            model_version=result.model_version,
            prompt_version=result.prompt_version,
            resource_usage=result.resource_usage,
            llm_io=_redacted_llm_io(result.llm_io),
        )
        CriticRunRepository(session).add(run)

        finding_id: str | None = None
        if result.result == CriticResultStatus.FINDING:
            finding = compose_finding(session, result=result, run=run)
            CriticRunRepository(session).link_finding(run.id, finding.id)
            finding_id = finding.id

        outcomes.append(
            CriticRunOutcome(
                critic_id=critic.critic_id,
                critic_version=critic.critic_version,
                result=result.result,
                proposition=result.proposition,
                rationale=result.rationale,
                finding_id=finding_id,
            )
        )
    return outcomes


def rerun_for_verification(
    session: Session,
    *,
    finding: Finding,
    clock: Callable[[], datetime] = utc_now,
) -> list[RerunOutcome]:
    """Re-run the critics that produced ``finding``, recording a new run for each.

    Verification must not be a silent re-assertion of the original conclusion, so this persists a
    fresh ``CriticRun`` per critic -- linked to the existing finding, but a separate row -- and does
    **not** compose a second finding. The original run is left untouched, so the two conclusions
    stay comparable.

    The critics are re-run against the finding's source event, which is where the evidence they
    assess lives. If the finding names no retrievable source event there is nothing to re-examine,
    and that is reported as an error rather than quietly returning an empty success.

    Note on independence: re-running the critic that raised the finding is *not* independent
    corroboration. It establishes whether the finding still reproduces, which is a weaker and
    different claim. Callers must not present it as independent verification.
    """

    linked = CriticRunRepository(session).list(finding_id=finding.id)
    critic_ids = sorted({run.critic_id for run in linked})
    if not critic_ids:
        raise CriticRegistryError(
            f"finding {finding.id!r} has no linked critic runs, so there is nothing to re-run"
        )

    event: Event | None = None
    for event_id in finding.event_ids:
        candidate = EventRepository(session).get(event_id)
        if candidate is not None:
            event = candidate
            break
    if event is None:
        raise CriticRegistryError(
            f"finding {finding.id!r} names no retrievable source event, so its critics cannot be "
            "re-run against current records"
        )

    context = _build_context(session, event, now=clock())
    outcomes: list[RerunOutcome] = []
    for critic_id in critic_ids:
        critic = get(critic_id)
        started_at = clock()
        failure_details: str | None = None
        try:
            result = critic.run(context)
        except Exception as exc:  # noqa: BLE001 - record the failure, redacted, and continue
            result = _error_result(critic, exc, context)
            failure_details = _redact_failure(exc)
        completed_at = clock()

        run = CriticRun(
            critic_id=critic.critic_id,
            critic_type=critic.critic_type,
            critic_version=critic.critic_version,
            input_refs=_input_refs(context),
            input_hashes=_input_hashes(context),
            started_at=started_at,
            completed_at=completed_at,
            result=result.result,
            rationale=result.rationale,
            failure_details=failure_details,
            finding_id=finding.id,
            model_provider=result.model_provider,
            model_version=result.model_version,
            prompt_version=result.prompt_version,
            resource_usage=result.resource_usage,
            llm_io=_redacted_llm_io(result.llm_io),
        )
        CriticRunRepository(session).add(run)
        outcomes.append(
            RerunOutcome(
                critic_id=critic.critic_id,
                critic_version=critic.critic_version,
                result=result.result,
                rationale=result.rationale,
                run_id=run.id,
            )
        )
    return outcomes


def _build_context(session: Session, event: Event, *, now: datetime) -> CriticContext:
    """Assemble the read-only context for one event from stored records."""

    claim = _linked_claim(session, event)
    related_claims = _related_claims(session, event, claim)
    evidence = _linked_evidence(session, event, claim)
    return build_context(
        target_event=event,
        linked_claim=claim,
        related_claims=related_claims,
        linked_evidence=evidence,
        now=now,
    )


def _linked_claim(session: Session, event: Event) -> Claim | None:
    payload_claim_id = event.payload.get("claim_id")
    if isinstance(payload_claim_id, str) and payload_claim_id:
        claim = ClaimRepository(session).get(payload_claim_id)
        if claim is not None:
            return claim
    for claim in ClaimRepository(session).list(claimant_system_id=event.source_system_id):
        if event.id in claim.source_event_ids:
            return claim
    return None


def _related_claims(session: Session, event: Event, claim: Claim | None) -> list[Claim]:
    """Return other stored claims about the same subject, for contradictory-record critics."""

    if claim is None:
        return []
    related: list[Claim] = []
    for candidate in ClaimRepository(session).list(
        claimant_system_id=event.source_system_id, limit=200
    ):
        if candidate.id == claim.id:
            continue
        if candidate.scope.artifact and candidate.scope.artifact == claim.scope.artifact:
            related.append(candidate)
    return related


def _linked_evidence(session: Session, event: Event, claim: Claim | None) -> list[EvidenceItem]:
    """Return evidence linked to the claim or referenced by the event payload."""

    evidence_ids: list[str] = []
    if claim is not None:
        for item in EvidenceRepository(session).list(relevance_to_claim=claim.id, limit=200):
            evidence_ids.append(item.id)
    payload_evidence_ids = event.payload.get("evidence_ids")
    if isinstance(payload_evidence_ids, list):
        for value in payload_evidence_ids:
            if isinstance(value, str) and value not in evidence_ids:
                evidence_ids.append(value)

    linked: list[EvidenceItem] = []
    for evidence_id in evidence_ids:
        found = EvidenceRepository(session).get(evidence_id)
        if found is not None:
            linked.append(found)
    return linked


def _input_refs(context: CriticContext) -> list[str]:
    refs = [context.target_event.id]
    if context.linked_claim is not None:
        refs.append(context.linked_claim.id)
    refs.extend(item.id for item in context.linked_evidence)
    return refs


def _input_hashes(context: CriticContext) -> list[str]:
    payloads: list[Any] = [context.target_event]
    if context.linked_claim is not None:
        payloads.append(context.linked_claim)
    payloads.extend(context.linked_evidence)
    return [content_hash(item) for item in payloads]


def _error_result(critic: Critic, exc: Exception, context: CriticContext) -> CriticResult:
    return CriticResult(
        critic_id=critic.critic_id,
        critic_version=critic.critic_version,
        result=CriticResultStatus.ERROR,
        proposition=f"{critic.critic_id} failed to execute",
        scope=context.scope,
        observations=[f"{critic.critic_id} raised {type(exc).__name__} during execution."],
        rationale=(
            f"{critic.critic_id} could not complete its assessment; the failure details are "
            "recorded redacted on the critic run."
        ),
        recommended_next_step="Inspect the critic run failure details and rerun the critic.",
    )


def _redacted_llm_io(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Redact a prompt/response payload at the persistence boundary.

    The critic may hand back the exact prompt it sent and the raw reply it received, which can
    contain user-supplied text. It runs through the same redaction rules as every other stored
    payload before it is written to ``critic_runs.llm_io``.
    """

    if payload is None:
        return None
    redacted, _ = redact(payload)
    return redacted if isinstance(redacted, dict) else None


def _redact_failure(exc: Exception) -> str:
    """Serialise the failure through the ingestion redaction pipeline.

    Exception text is untrusted: it may echo payloads or credentials. It passes through the
    same redaction rules before it is allowed into the stored failure details.
    """

    redacted, _ = redact(
        {"error_type": type(exc).__name__, "message": str(exc), "critic_run": True}
    )
    return json.dumps(redacted, sort_keys=True, ensure_ascii=False)
