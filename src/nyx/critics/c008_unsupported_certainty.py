"""C-008 — Unsupported certainty (SPEC §8).

Detects a claim whose wording is stronger than its attached evidence scope — the
canonical case is a universal statement ("all tests pass") backed by a single test run.

Implemented as explicit rules, not vibes:

* RULE 1 (universal wording): the claim statement contains a universal quantifier marker
  (all, every, always, never, none, 100%, guaranteed, fully, completely, entire).
* RULE 2 (thin evidence): the claim is linked to exactly one verification run
  (test_result or ci_test_result evidence).

Both rules must hold. Universal wording backed by two or more linked verification runs is
not fired on: the evidence scope is broader than a single run, and whether it covers the
asserted universality is a judgement this critic does not make. Universal wording with no
linked verification is out of scope here — missing verification is C-001's finding, not an
unsupported-certainty finding.

The output is an ASSESSMENT of wording-versus-evidence proportion. It is never labelled
deterministic verification of truth: the critic cannot tell whether the claim is false,
only that its attached support is thinner than its wording. No model assistance is used;
a future optional model check would be additive and would still not be final proof.

A ``no_finding`` result means only that this critic found no wording-versus-evidence gap
within its declared scope; it is never a statement that the claim is true, or that no
other critic could find something.
"""

from __future__ import annotations

import re

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    EvidenceKind,
    FindingCategory,
    Severity,
)

CRITIC_ID = "c008-unsupported-certainty"
CRITIC_VERSION = "1.0.0"

_VERIFICATION_KINDS = {EvidenceKind.TEST_RESULT, EvidenceKind.CI_TEST_RESULT}

_UNIVERSAL_MARKER = re.compile(
    r"\b(all|every|always|never|none|100%|guaranteed|fully|completely|entire)\b"
)


def _asserts_universal(claim: Claim) -> bool:
    """RULE 1: does the claim statement contain a universal quantifier marker?"""

    return bool(_UNIVERSAL_MARKER.search(claim.statement.lower()))


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no issue within its declared scope. It
    is never a statement that the claim is true, that the evidence proves it, or that no
    other critic could find something.
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


class UnsupportedCertaintyCritic:
    """Assess whether a claim's wording outruns its attached evidence scope."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Detect claims whose wording is stronger than their attached evidence scope, "
        "using explicit rules; the output is an assessment, never verification of truth."
    )
    scope = CriticScope(
        can_assess=[
            "universal quantifier markers in a linked claim's statement",
            "the count of verification runs linked to the claim",
        ],
        cannot_assess=[
            "claims with no linked evidence (missing verification is the completion "
            "critic's finding)",
            "claims backed by two or more linked verification runs",
            "per-test coverage inside a single run",
            "whether the claim is actually true or false",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def run(self, context: CriticContext) -> CriticResult:
        claim = context.linked_claim
        if claim is None:
            return _no_finding(
                "No claim is linked to this event.",
                "This critic assesses a linked claim's wording against its attached evidence "
                "scope; without a linked claim there is nothing to assess within this "
                "critic's declared scope.",
                context.scope,
            )

        verification = [
            item for item in context.linked_evidence if item.kind in _VERIFICATION_KINDS
        ]
        if not verification:
            return _no_finding(
                f"Claim {claim.id} has no linked verification evidence.",
                "This critic assesses wording against attached evidence scope; with no "
                "linked verification runs there is no evidence scope to compare, and "
                "missing verification is the completion critic's finding, not this "
                "critic's.",
                context.scope,
            )
        if len(verification) > 1:
            return _no_finding(
                f"Claim {claim.id} is backed by {len(verification)} linked verification runs.",
                "The evidence scope is broader than a single run, so this explicit rule "
                "cannot show the wording outruns the evidence. This critic found nothing "
                "within its declared scope.",
                context.scope,
            )
        if not _asserts_universal(claim):
            return _no_finding(
                f"Claim {claim.id} does not assert universal scope.",
                "The claim's wording is proportionate to (no stronger than) a single "
                "verification run, so this critic found nothing within its declared scope; "
                "this is not a statement that the claim is true.",
                context.scope,
            )

        evidence = verification[0]
        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"Claim {claim.id} asserts universal scope but is supported by a single "
                f"verification run ({evidence.id})."
            ),
            scope=claim.scope,
            observations=[
                f'Claim {claim.id} states: "{claim.statement}"',
                f"Exactly one verification run is linked: {evidence.id} "
                f'({evidence.kind.value}) "{evidence.title}".',
            ],
            inferences=[
                "A single verification run cannot establish a universal statement over all "
                "tests, environments, or conditions the claim asserts.",
            ],
            limitations=[
                "This is a rule-based assessment of wording-versus-evidence proportion, "
                "not a verification that the claim is true or false.",
                "Evidence scope is inferred from the count of linked verification runs; "
                "per-test coverage inside the single run is not read.",
                "Optional model assistance may be added later; it would remain an "
                "assessment, never final proof.",
            ],
            rationale=(
                f"The wording of claim {claim.id} asserts universality, but its attached "
                f"evidence covers a single verification run ({evidence.id}). The evidence "
                "scope is thinner than the wording, so the claim's certainty is unsupported "
                "by what is linked. This is an assessment of proportion, not proof that "
                "the claim is false."
            ),
            recommended_next_step=(
                "Either narrow the claim's wording to the tested scope, or attach "
                "verification evidence that covers the full asserted scope (the complete "
                "suite across all claimed environments)."
            ),
            category=FindingCategory.UNSUPPORTED_CLAIM,
            severity=Severity.MEDIUM,
            title=f"Unsupported certainty in claim {claim.id}",
            summary=(
                f"Claim {claim.id} asserts universal scope but is supported by one "
                f"verification run ({evidence.id}); the wording outruns the evidence."
            ),
            supporting_evidence_ids=[evidence.id],
            claim_ids=[claim.id],
            event_ids=[context.target_event.id],
            affected_system_ids=[claim.claimant_system_id],
        )
