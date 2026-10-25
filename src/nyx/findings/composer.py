"""Turn a ``CriticResult`` into a persisted ``Finding`` (SPEC §5.5, §3.2 invariant 1).

The composer is the trust boundary between a critic and the finding store. A critic's output
is an *assessment*, never a verified fact, so the composer validates before persisting:

1. **Evidence references must exist.** Every id in ``supporting_evidence_ids`` and
   ``counterevidence_ids`` is looked up in storage. Invented or stale references are
   rejected with the unknown ids named; the composer never trusts the critic's references.
2. **Category-required fields must be present.** For example a ``contradiction`` finding
   must state its contradiction, and an ``unsupported_claim`` finding must cite the evidence
   it examined.
3. **Safety invariant 1.** A finding may only be composed as ``confirmed`` when it carries
   a rationale and at least one supporting evidence item.
4. **A finding must be inspectable.** It must reference the claim or event it assesses,
   record at least one observation, and keep observations, inferences, recommendations, and
   unknowns in their separate fields.

After validation the finding is deduplicated by identity (never by prose similarity) and
persisted. Any failure raises :class:`FindingCompositionError` before a row is written.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from nyx.critics.base import CriticResult
from nyx.domain.enums import (
    CriticResultStatus,
    FindingCategory,
    FindingStatus,
    Severity,
)
from nyx.findings.dedup import TargetType, dedupe_key, find_original, mark_as_duplicate
from nyx.findings.models import (
    CriticRun,
    Finding,
    Reasoning,
    RecommendedAction,
)
from nyx.storage.repositories.evidence import EvidenceRepository
from nyx.storage.repositories.findings import FindingRepository
from nyx.storage.repositories.observations import ObservationRepository


class FindingCompositionError(ValueError):
    """A critic result cannot be composed into a valid finding."""


_CATEGORY_REQUIRED_REASONING: dict[FindingCategory, tuple[str, ...]] = {
    FindingCategory.CONTRADICTION: ("contradiction",),
}


def compose_finding(
    session: Session,
    *,
    result: CriticResult,
    run: CriticRun,
    requested_status: FindingStatus | None = None,
) -> Finding:
    """Validate ``result`` and persist the composed finding.

    ``requested_status`` lets a caller ask for a lifecycle status above ``detected``; asking
    for ``confirmed`` triggers the safety-invariant-1 checks. The run must already be
    persisted; its ``finding_id`` back-reference is set once the finding exists.
    """

    if result.result != CriticResultStatus.FINDING:
        raise FindingCompositionError(
            f"critic {result.critic_id} returned {result.result.value}; only 'finding' results "
            "can be composed."
        )
    _validate_evidence_references(session, result)
    _validate_observation_references(session, result)
    _validate_category_requirements(result)
    _validate_inspectable(result)

    target_type, target_id = _target_identity(result)
    key = dedupe_key(
        critic_id=result.critic_id,
        target_type=target_type,
        target_id=target_id,
        scope=result.scope,
    )

    status = requested_status or FindingStatus.DETECTED
    if status is FindingStatus.CONFIRMED:
        _validate_confirmed_invariant(result)

    evidence_ids = _union_ids(result.supporting_evidence_ids, result.counterevidence_ids)
    affected = list(result.affected_system_ids)
    if result.scope.system_id not in affected:
        affected.append(result.scope.system_id)

    finding = Finding(
        title=result.title or result.proposition[:200],
        summary=result.summary or result.proposition,
        category=result.category or FindingCategory.OTHER,
        severity=result.severity or Severity.MEDIUM,
        confidence=result.confidence,
        scope=result.scope,
        affected_system_ids=affected,
        claim_ids=list(result.claim_ids),
        event_ids=list(result.event_ids),
        evidence_ids=evidence_ids,
        supporting_evidence_ids=list(result.supporting_evidence_ids),
        counterevidence_ids=list(result.counterevidence_ids),
        reasoning=Reasoning(
            observations=list(result.observations),
            inferences=list(result.inferences),
            premises=list(result.premises),
            contradiction=result.contradiction,
        ),
        limitations=list(result.limitations),
        unknowns=list(result.unknowns),
        recommended_action=RecommendedAction(text=result.recommended_next_step),
        critic_runs=[run],
        status=status,
        dedupe_key=key,
        observation_ids=list(result.observation_refs),
        root_cause_key=result.root_cause_key,
        subject_version=result.subject_version,
        reproducibility=result.reproducibility,
        verification_strength=result.verification_strength,
        remediation_hint=result.remediation_hint,
    )

    original = find_original(session=session, key=key)
    if original is not None and original.id != finding.id:
        finding = mark_as_duplicate(finding, original, key)

    FindingRepository(session).add(finding)
    return finding


def _validate_evidence_references(session: Session, result: CriticResult) -> None:
    referenced = _union_ids(result.supporting_evidence_ids, result.counterevidence_ids)
    if not referenced:
        return
    unknown = [
        evidence_id
        for evidence_id in referenced
        if EvidenceRepository(session).get(evidence_id) is None
    ]
    if unknown:
        raise FindingCompositionError(
            f"critic {result.critic_id} referenced evidence IDs that do not exist: "
            f"{', '.join(unknown)}. A critic must reference persisted evidence ids."
        )


def _validate_observation_references(session: Session, result: CriticResult) -> None:
    """Observation references must exist too.

    The rule the brief states is that no finding may reference something that does not exist, and
    an observation id is a reference like any other. The check is separate from the evidence check
    because the two resolve against different tables, and a single combined message would hide
    which kind of reference was invented.
    """

    if not result.observation_refs:
        return
    unknown = [
        observation_id
        for observation_id in result.observation_refs
        if ObservationRepository(session).get(observation_id) is None
    ]
    if unknown:
        raise FindingCompositionError(
            f"critic {result.critic_id} referenced observation IDs that do not exist: "
            f"{', '.join(unknown)}. A critic must reference persisted observation ids."
        )


def _validate_category_requirements(result: CriticResult) -> None:
    required = _CATEGORY_REQUIRED_REASONING.get(result.category or FindingCategory.OTHER, ())
    for field_name in required:
        value = getattr(result, field_name, None)
        if not value:
            raise FindingCompositionError(
                f"a {result.category.value if result.category else 'other'} finding must "
                f"provide '{field_name}'."
            )
    if result.category is FindingCategory.UNSUPPORTED_CLAIM and not _union_ids(
        result.supporting_evidence_ids, result.counterevidence_ids
    ):
        raise FindingCompositionError(
            "an unsupported_claim finding must cite at least one evidence item it examined."
        )


def _validate_inspectable(result: CriticResult) -> None:
    if not result.observations:
        raise FindingCompositionError(
            "a finding must record at least one observation; an empty observation list is "
            "not inspectable."
        )
    if not result.claim_ids and not result.event_ids:
        raise FindingCompositionError("a finding must reference the claim or event it assesses.")


def _validate_confirmed_invariant(result: CriticResult) -> None:
    if not result.supporting_evidence_ids:
        raise FindingCompositionError(
            "safety invariant 1: a finding may not be composed as 'confirmed' without at "
            "least one supporting evidence item."
        )
    if not result.rationale:
        raise FindingCompositionError(
            "safety invariant 1: a finding may not be composed as 'confirmed' without a "
            "recorded rationale."
        )


def _target_identity(result: CriticResult) -> tuple[TargetType, str]:
    if result.claim_ids:
        return "claim", result.claim_ids[0]
    return "event", result.event_ids[0]


def _union_ids(left: list[str], right: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for value in (*left, *right):
        if value not in seen:
            seen.add(value)
            merged.append(value)
    return merged
