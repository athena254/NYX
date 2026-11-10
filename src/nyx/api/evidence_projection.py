"""Principal-specific projections for responses derived from evidence.

Stored findings and dispute records remain append-only. This module only creates safe
HTTP-facing copies when a referenced evidence record is not readable by the caller.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from nyx.api.routes.evidence import required_capability_for_access_policy
from nyx.auth.principal import AuthenticatedPrincipal
from nyx.domain.enums import Sensitivity
from nyx.findings.lifecycle import Capability
from nyx.findings.models import CriticRun, Finding, Reasoning, RecommendedAction
from nyx.storage.repositories.claims import ClaimRepository
from nyx.storage.repositories.events import EventRepository
from nyx.storage.repositories.evidence import EvidenceRepository

_MASKED = "Details are omitted because this record depends on evidence the caller cannot read."


def finding_depends_on_unreadable_evidence(
    session: Session, finding: Finding, principal: AuthenticatedPrincipal
) -> bool:
    """Fail closed when a finding or any recorded critic input cites unreadable evidence."""

    if {
        Capability.EVIDENCE_READ,
        Capability.EVIDENCE_RESTRICTED,
    }.issubset(principal.capabilities):
        return False

    evidence_ids = set(finding.evidence_ids)
    for run in finding.critic_runs:
        evidence_ids.update(
            reference for reference in run.input_refs if reference.startswith("ev_")
        )

    repository = EvidenceRepository(session)
    if evidence_ids and Capability.EVIDENCE_READ not in principal.capabilities:
        return True
    for evidence_id in evidence_ids:
        item = repository.get(evidence_id)
        if item is None:
            return True
        try:
            restricted_by_policy = (
                required_capability_for_access_policy(item.access_policy_ref)
                is Capability.EVIDENCE_RESTRICTED
            )
        except ValueError:
            return True
        if item.sensitivity is Sensitivity.RESTRICTED or restricted_by_policy:
            return True
    return False


def event_depends_on_unreadable_evidence(
    session: Session, event_id: str, principal: AuthenticatedPrincipal
) -> bool:
    """Inspect a critic event's claim lineage before returning derived critic text."""

    event = EventRepository(session).get(event_id)
    if event is None:
        return True
    claim_id = event.payload.get("claim_id")
    if not isinstance(claim_id, str):
        return False
    if ClaimRepository(session).get(claim_id) is None:
        return True
    repository = EvidenceRepository(session)
    evidence_ids = [item.id for item in repository.list(relevance_to_claim=claim_id)]
    if not evidence_ids:
        return False
    if {
        Capability.EVIDENCE_READ,
        Capability.EVIDENCE_RESTRICTED,
    }.issubset(principal.capabilities):
        return False
    for evidence_id in evidence_ids:
        item = repository.get(evidence_id)
        if item is None:
            return True
        try:
            restricted_by_policy = (
                required_capability_for_access_policy(item.access_policy_ref)
                is Capability.EVIDENCE_RESTRICTED
            )
        except ValueError:
            return True
        if item.sensitivity is Sensitivity.RESTRICTED or restricted_by_policy:
            return True
    return False


def project_finding(finding: Finding, *, mask_derived_content: bool) -> Finding:
    """Return a response copy with all finding-derived prose masked when needed."""

    if not mask_derived_content:
        return finding
    runs = [
        run.model_copy(
            update={
                "rationale": _MASKED,
                "failure_details": None,
                "llm_io": None,
                "input_hashes": [],
            }
        )
        for run in finding.critic_runs
    ]
    return finding.model_copy(
        update={
            "title": "Finding details withheld",
            "summary": _MASKED,
            "reasoning": Reasoning(
                observations=[_MASKED],
                inferences=[_MASKED],
                premises=[_MASKED],
                contradiction=_MASKED,
                reproduction=_MASKED,
            ),
            "recommended_action": RecommendedAction(
                text=_MASKED,
                requires_authorization=True,
                authority=None,
            ),
            "critic_runs": runs,
            "dedupe_rationale": _MASKED if finding.dedupe_rationale else None,
            "limitations": [
                _MASKED,
                *[
                    f"Evidence {evidence_id} is restricted or otherwise unreadable; "
                    "details omitted."
                    for evidence_id in finding.evidence_ids[:31]
                ],
            ],
            "unknowns": [_MASKED] if finding.unknowns else [],
            "remediation_hint": _MASKED if finding.remediation_hint else None,
        }
    )


def project_critic_run(run: CriticRun, *, mask_derived_content: bool) -> CriticRun:
    """Return a run copy that retains provenance but not rationale derived from hidden input."""

    if not mask_derived_content:
        return run
    return run.model_copy(update={"rationale": _MASKED, "failure_details": None, "llm_io": None})


def masked_text(value: str | None, *, mask_derived_content: bool) -> str | None:
    """Project one derived free-text field without altering its persisted source."""

    return _MASKED if mask_derived_content and value is not None else value
