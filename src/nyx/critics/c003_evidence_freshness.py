"""C-003 — Evidence freshness (SPEC §8).

Flags evidence whose declared scope or validity window does not cover the claim being
evaluated. A test report captured for an older commit, or captured outside the claim's
validity window, cannot silently verify a newer artifact — the critic says so explicitly
instead of letting the evidence pass as coverage.

The critic only fires on **declared** non-coverage: a scope field that is set on both the
evidence and the claim and differs, or a validity window that provably ends before (or
starts after) the claim's evaluation time. An evidence item that declares no scope is not
accused here; missing provenance is C-004's finding.
"""

from __future__ import annotations

from datetime import datetime

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import CriticResultStatus, CriticType, FindingCategory, Severity
from nyx.domain.evidence import EvidenceItem

CRITIC_ID = "c003-evidence-freshness"
CRITIC_VERSION = "1.0.0"


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no issue within its declared scope. It
    is never a statement that the evidence proves the claim, or that no other critic could
    find something.
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


class EvidenceFreshnessCritic:
    """Check that linked evidence covers the claim's scope and evaluation time."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Flag evidence whose declared scope or validity window does not cover the claim "
        "being evaluated."
    )
    scope = CriticScope(
        can_assess=[
            "validity windows of evidence linked to a claim",
            "declared scope fields of evidence linked to a claim",
        ],
        cannot_assess=[
            "evidence that declares no scope (handled by the provenance-gap critic)",
            "the truth of the evidence content",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def run(self, context: CriticContext) -> CriticResult:
        claim = context.linked_claim
        if claim is None:
            return _no_finding(
                "No claim is linked to this event.",
                "This critic assesses the freshness of evidence linked to a claim; without a "
                "linked claim there is nothing to assess.",
                context.scope,
            )
        if not context.linked_evidence:
            return _no_finding(
                f"No evidence is linked to claim {claim.id}.",
                "This critic assesses the freshness of linked evidence; with no linked "
                "evidence there is nothing to assess within this critic's declared scope.",
                context.scope,
            )

        claim_time = self._claim_time(claim)
        claim_end = self._claim_end(claim)

        covering = [
            item
            for item in context.linked_evidence
            if self._covers(item, claim, claim_time, claim_end)
        ]
        if covering:
            return _no_finding(
                f"Evidence linked to claim {claim.id} covers the claim's scope and time.",
                f"Linked evidence {covering[0].id} declares a scope and validity window that "
                "cover the claim's evaluation time. This critic found no issue within its "
                "declared scope; coverage is not proof that the claim is true.",
                context.scope,
            )

        stale = self._stale_older_commit(context.linked_evidence, claim)
        if stale is not None:
            evidence = stale
            return self._finding(
                claim,
                proposition=(
                    f"Evidence {evidence.id} was captured for an older commit than claim "
                    f"{claim.id} and cannot verify it."
                ),
                observations=[
                    f"Claim {claim.id} names commit {claim.scope.commit} and was made at "
                    f"{claim.created_at}.",
                    f"Evidence {evidence.id} declares commit "
                    f"{evidence.scope.commit if evidence.scope else '<unknown>'} and was "
                    f"captured at {evidence.captured_at}, before the claim was made.",
                ],
                rationale=(
                    f"Evidence {evidence.id} targets commit "
                    f"{evidence.scope.commit if evidence.scope else '<unknown>'}, not the "
                    f"claimed commit {claim.scope.commit}, and was captured before the claim "
                    "was made. Stale evidence cannot silently verify a newer artifact."
                ),
                title=f"Stale evidence for older commit linked to claim {claim.id}",
            )

        window_gap = self._window_gap(context.linked_evidence, claim, claim_time, claim_end)
        if window_gap is not None:
            evidence, detail = window_gap
            return self._finding(
                claim,
                proposition=(
                    f"Evidence {evidence.id} does not cover the validity window of claim "
                    f"{claim.id}."
                ),
                observations=[
                    f"Claim {claim.id} is evaluated at {claim_time}"
                    + (f" through {claim_end}." if claim_end else "."),
                    f"Evidence {evidence.id} is valid from {evidence.valid_from} to "
                    f"{evidence.valid_until or 'unbounded'}: {detail}.",
                ],
                rationale=(
                    f"The validity window of evidence {evidence.id} does not cover the "
                    f"claim's evaluation time ({detail}), so the evidence cannot verify the "
                    "claim as scoped."
                ),
                title=f"Evidence window does not cover claim {claim.id}",
            )

        mismatch = self._scope_mismatch(context.linked_evidence, claim)
        if mismatch is not None:
            evidence, field, evidence_value, claim_value = mismatch
            return self._finding(
                claim,
                proposition=(
                    f"Evidence {evidence.id} declares a {field} that does not cover claim "
                    f"{claim.id}."
                ),
                observations=[
                    f"Claim {claim.id} declares {field}={claim_value}.",
                    f"Evidence {evidence.id} declares {field}={evidence_value}.",
                ],
                rationale=(
                    f"Evidence {evidence.id} declares {field}={evidence_value} while the "
                    f"claim declares {field}={claim_value}, so the evidence's declared scope "
                    "does not cover the claim."
                ),
                title=f"Evidence scope mismatch on {field} for claim {claim.id}",
            )

        return _no_finding(
            f"Evidence linked to claim {claim.id} does not declare a conflicting scope.",
            "No linked evidence declares a scope or validity window that provably fails to "
            "cover the claim. This critic found nothing within its declared scope.",
            context.scope,
        )

    def _finding(
        self, claim: Claim, *, proposition: str, observations: list[str], rationale: str, title: str
    ) -> CriticResult:
        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=proposition,
            scope=claim.scope,
            observations=observations,
            limitations=[
                "A non-covering artifact is a freshness/scope observation, not evidence of "
                "misconduct.",
            ],
            rationale=rationale,
            recommended_next_step=(
                "Link evidence whose declared scope and validity window cover the claim."
            ),
            category=FindingCategory.DRIFT,
            severity=Severity.MEDIUM,
            title=title,
            summary=f"{proposition} This is a freshness/scope observation, not a defect assertion.",
            claim_ids=[claim.id],
            event_ids=[],
            affected_system_ids=[claim.claimant_system_id],
        )

    @staticmethod
    def _claim_time(claim: Claim) -> datetime:
        if claim.scope.period is not None:
            return claim.scope.period.from_
        return claim.created_at

    @staticmethod
    def _claim_end(claim: Claim) -> datetime | None:
        if claim.scope.period is not None:
            return claim.scope.period.to
        return None

    @staticmethod
    def _covers(
        item: EvidenceItem,
        claim: Claim,
        claim_time: datetime,
        claim_end: datetime | None,
    ) -> bool:
        if not EvidenceFreshnessCritic._scope_fields_match(item, claim):
            return False
        if item.valid_from > claim_time:
            return False
        if item.valid_until is not None and item.valid_until < claim_time:
            return False
        return not (
            claim_end is not None and item.valid_until is not None and item.valid_until < claim_end
        )

    @staticmethod
    def _stale_older_commit(items: list[EvidenceItem], claim: Claim) -> EvidenceItem | None:
        if not claim.scope.commit:
            return None
        for item in items:
            if item.scope is None or not item.scope.commit:
                continue
            if item.scope.commit != claim.scope.commit and item.captured_at < claim.created_at:
                return item
        return None

    @staticmethod
    def _window_gap(
        items: list[EvidenceItem],
        claim: Claim,
        claim_time: datetime,
        claim_end: datetime | None,
    ) -> tuple[EvidenceItem, str] | None:
        for item in items:
            if item.valid_from > claim_time:
                return item, "the evidence starts being valid after the claim's evaluation time"
            if item.valid_until is not None and item.valid_until < claim_time:
                return item, "the evidence expired before the claim's evaluation time"
            if (
                claim_end is not None
                and item.valid_until is not None
                and item.valid_until < claim_end
            ):
                return item, "the evidence expires before the claim's period ends"
        return None

    @staticmethod
    def _scope_mismatch(
        items: list[EvidenceItem], claim: Claim
    ) -> tuple[EvidenceItem, str, str, str] | None:
        for item in items:
            if item.scope is None:
                continue
            for field in ("commit", "artifact", "environment"):
                evidence_value = getattr(item.scope, field)
                claim_value = getattr(claim.scope, field)
                if evidence_value and claim_value and evidence_value != claim_value:
                    return item, field, evidence_value, claim_value
        return None

    @staticmethod
    def _scope_fields_match(item: EvidenceItem, claim: Claim) -> bool:
        """Return True unless a scope field is set on both sides and differs."""

        if item.scope is None:
            return True
        for field in ("commit", "artifact", "environment"):
            evidence_value = getattr(item.scope, field)
            claim_value = getattr(claim.scope, field)
            if evidence_value and claim_value and evidence_value != claim_value:
                return False
        return True
