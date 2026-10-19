"""C-004 — Provenance gap (SPEC §8).

Flags material claims whose linked evidence lacks a source, source identity, locator,
capture time, or scope. The finding **names the missing fields** and is worded as a
completeness gap: it never implies malicious behaviour. A record with missing provenance is
harder to audit; it is not evidence of wrongdoing.
"""

from __future__ import annotations

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Scope
from nyx.domain.enums import CriticResultStatus, CriticType, FindingCategory, Severity
from nyx.domain.evidence import EvidenceItem

CRITIC_ID = "c004-provenance-gap"
CRITIC_VERSION = "1.0.0"

_REQUIRED_PROVENANCE: tuple[tuple[str, str], ...] = (
    ("source", "a source_system_id or source_name"),
    ("source identity", "producer_identity"),
    ("locator", "locator"),
    ("capture time", "captured_at"),
    ("scope", "scope"),
)


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no issue within its declared scope. It
    is never a statement that the evidence is authentic or that the claim is true.
    """

    return CriticResult(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        result=CriticResultStatus.NO_FINDING,
        proposition=proposition,
        scope=scope,
        observations=[],
        rationale=rationale,
        recommended_next_step="No action required from this critic.",
    )


def _missing_fields(evidence: EvidenceItem) -> list[str]:
    """Return the human-readable names of the provenance fields the evidence is missing."""

    missing: list[str] = []
    if evidence.source_system_id is None and evidence.source_name is None:
        missing.append("source")
    if evidence.producer_identity is None:
        missing.append("source identity")
    if evidence.locator is None:
        missing.append("locator")
    if evidence.captured_at is None:
        missing.append("capture time")  # type: ignore[unreachable]  # model types this as non-optional but code defensively checks
    if evidence.scope is None:
        missing.append("scope")
    return missing


class ProvenanceGapCritic:
    """Check that linked evidence carries the provenance required to audit it."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Flag material claims whose evidence lacks a source, source identity, locator, "
        "capture time, or scope, naming the missing fields without implying malice."
    )
    scope = CriticScope(
        can_assess=[
            "provenance completeness of evidence linked to a claim",
        ],
        cannot_assess=[
            "the authenticity or truth of evidence content",
            "the intent of the evidence producer",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def run(self, context: CriticContext) -> CriticResult:
        claim = context.linked_claim
        if claim is None:
            return _no_finding(
                "No claim is linked to this event.",
                "This critic assesses the provenance of evidence linked to a claim; without a "
                "linked claim there is nothing to assess.",
                context.scope,
            )
        if not context.linked_evidence:
            return _no_finding(
                f"No evidence is linked to claim {claim.id}.",
                "This critic assesses the provenance of linked evidence; with no linked "
                "evidence there is nothing to assess within this critic's declared scope.",
                context.scope,
            )

        gaps: list[tuple[EvidenceItem, list[str]]] = [
            (item, _missing_fields(item))
            for item in context.linked_evidence
            if _missing_fields(item)
        ]
        if not gaps:
            return _no_finding(
                f"Evidence linked to claim {claim.id} carries the required provenance.",
                "Every linked evidence item declares a source, source identity, locator, "
                "capture time, and scope. This critic found no issue within its declared "
                "scope; provenance completeness is not authenticity.",
                context.scope,
            )

        observations = [
            f"Claim {claim.id} is linked to {len(context.linked_evidence)} evidence item(s)."
        ]
        for item, missing in gaps:
            observations.append(f"Evidence {item.id} is missing: {', '.join(missing)}.")
        named = "; ".join(f"{item.id} lacks {', '.join(missing)}" for item, missing in gaps)

        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"Evidence linked to claim {claim.id} is missing required provenance fields."
            ),
            scope=claim.scope,
            observations=observations,
            inferences=[
                "The affected evidence items cannot be fully audited until the missing "
                "provenance is supplied.",
            ],
            limitations=[
                "This is a completeness gap in the evidence record, not an assertion of "
                "malicious behaviour by the producer.",
            ],
            rationale=(
                f"Claim {claim.id} is linked to evidence with incomplete provenance: {named}. "
                "The missing fields are named so the producer can supply them; no intent is "
                "implied."
            ),
            recommended_next_step=(
                "Ask the evidence producer to supply the missing provenance fields."
            ),
            category=FindingCategory.PROVENANCE_GAP,
            severity=Severity.LOW,
            title=f"Provenance gap in evidence linked to claim {claim.id}",
            summary=(
                f"Evidence linked to claim {claim.id} is missing provenance fields: {named}. "
                "This is a completeness gap, not an accusation."
            ),
            supporting_evidence_ids=[item.id for item, _ in gaps],
            claim_ids=[claim.id],
            event_ids=[],
            affected_system_ids=[claim.claimant_system_id],
        )
