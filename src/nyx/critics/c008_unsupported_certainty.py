"""C-008 — Unsupported certainty (SPEC §8).

Detects a claim whose wording is stronger than its attached evidence scope — the
canonical case is a universal statement ("all tests pass") backed by a single test run.

Implemented as explicit rules, not vibes:

* RULE 1 (universal wording): the claim statement contains a universal quantifier marker
  (all, every, always, never, none, 100%, guaranteed, fully, completely, entire).
* RULE 2 (thin evidence): the claim is linked to exactly one verification run
  (test_result or ci_test_result evidence).
* RULE 3 (overconfidence vs verification strength): the claim states a numeric confidence,
  and that number exceeds the ceiling its backing support can honestly carry.

RULES 1 and 2 must both hold for a RULE 1/2 finding. Universal wording backed by two or more
linked verification runs is not fired on: the evidence scope is broader than a single run,
and whether it covers the asserted universality is a judgement this critic does not make.
Universal wording with no linked verification is out of scope here — missing verification is
C-001's finding, not an unsupported-certainty finding.

RULE 3 is independent of RULES 1 and 2, because a claim can be modest in wording and still
state a number its support cannot carry. The confidence is read from the claim's own statement
(a percentage), or from the target event's ``confidence`` field when the statement carries
none. The ceiling comes from :func:`nyx.evidence.verification.evidence_supports`, so the
strength is decided by the same mapping every other critic uses rather than re-derived here.

The ceilings in ``_CONFIDENCE_CEILING`` are a *declared policy*, not a measurement. They are
the honest maximum a given strength can support, and they are deliberately uncalibrated
against real agent behaviour: the per-agent calibration this brief also asks for needs a
persisted history of past claims and their outcomes, which a pure critic over a read-only
context cannot hold. That half is a documented gap, not a silent omission.

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
    VerificationStrength,
)
from nyx.evidence.verification import evidence_supports

CRITIC_ID = "c008-unsupported-certainty"
CRITIC_VERSION = "1.1.0"

_VERIFICATION_KINDS = {EvidenceKind.TEST_RESULT, EvidenceKind.CI_TEST_RESULT}

_UNIVERSAL_MARKER = re.compile(
    r"\b(all|every|always|never|none|100%|guaranteed|fully|completely|entire)\b"
)

#: A stated percentage, with an optional decimal part. ``\b`` on both sides so ``1000%`` is not
#: read as ``100%``.
_PERCENTAGE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*%")

#: The maximum confidence each verification strength can honestly support, as an integer
#: percentage. This is a declared policy, not a measurement: the numbers are the ceiling a
#: reader should treat as defensible, and they are deliberately uncalibrated against real
#: agent behaviour (see the module docstring).
#:
#: * ``NONE`` — nothing backs the claim, so no number is defensible. 30 is a floor, not a
#:   licence: it exists so an unsourced claim is not automatically a finding, while any real
#:   confidence figure still exceeds it.
#: * ``SELF_REPORTED`` — the claimant's own record. A producer can honestly report that it
#:   believes something; it cannot honestly claim near-certainty about a state it did not
#:   observe. 50 marks "better than a coin flip, worse than established".
#: * ``RERUN_BY_NYX`` — NYX re-executed the check itself at the current version. Independent of
#:   the claimant, but still reading the same artifact, so 85 allows for a flaky harness or a
#:   narrow rerun scope.
#: * ``INDEPENDENTLY_OBSERVED_END_STATE`` — NYX observed the real end state. 99 rather than 100
#:   because an observation is a sample of the environment at one moment, not a proof.
_CONFIDENCE_CEILING: dict[VerificationStrength, int] = {
    VerificationStrength.NONE: 30,
    VerificationStrength.SELF_REPORTED: 50,
    VerificationStrength.RERUN_BY_NYX: 85,
    VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE: 99,
}


def _asserts_universal(claim: Claim) -> bool:
    """RULE 1: does the claim statement contain a universal quantifier marker?"""

    return bool(_UNIVERSAL_MARKER.search(claim.statement.lower()))


def _stated_confidence(claim: Claim, context: CriticContext) -> int | None:
    """Read the numeric confidence the claim asserts, or ``None`` when it states none.

    The claim's own statement is read first, because that is the words a reader will hold the
    claimant to. The target event's ``confidence`` field is the fallback, for a producer that
    records the number structurally rather than in prose. A value above 100 is clamped to 100:
    a producer that writes ``150%`` is not making a stronger claim than one that writes
    ``100%``, and reading it literally would turn a typo into a different finding.
    """

    match = _PERCENTAGE.search(claim.statement)
    if match is not None:
        return min(int(float(match.group(1))), 100)

    raw = context.target_event.payload.get("confidence")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return min(int(raw), 100)
    if isinstance(raw, str):
        match = _PERCENTAGE.search(raw)
        if match is not None:
            return min(int(float(match.group(1))), 100)
    return None


def _backing_strength(claim: Claim, context: CriticContext) -> VerificationStrength:
    """The strongest support actually linked to this claim, on the shared strength scale."""

    return evidence_supports(
        claim.id,
        evidence=context.linked_evidence,
        observations=context.observations,
    )


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
            "a numeric confidence stated by the claim or its target event",
            "the strongest verification strength linked to the claim, on the shared scale",
        ],
        cannot_assess=[
            "claims with no linked evidence (missing verification is the completion "
            "critic's finding)",
            "claims backed by two or more linked verification runs",
            "per-test coverage inside a single run",
            "whether the claim is actually true or false",
            "per-agent calibration over time: that needs a persisted history of past claims "
            "and their outcomes, which a critic over a read-only context cannot hold. The "
            "ceilings are a declared policy, not a measurement of how this agent actually "
            "behaves.",
            "a claim that states no numeric confidence: RULE 3 has nothing to compare",
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

        wording = self._wording_vs_evidence(claim, context)
        if wording is not None:
            return wording

        return self._overconfidence(claim, context) or _no_finding(
            f"Claim {claim.id} states no confidence above its support's ceiling.",
            "The claim's wording is proportionate to its evidence scope and, where it states "
            "a numeric confidence, that number is within the ceiling its linked support can "
            "carry. This critic found nothing within its declared scope; this is not a "
            "statement that the claim is true.",
            context.scope,
        )

    def _wording_vs_evidence(self, claim: Claim, context: CriticContext) -> CriticResult | None:
        """RULES 1 and 2: universal wording backed by a single verification run.

        Run **before** RULE 3 on purpose. A claim can be both universal and overconfident,
        and when it is, the wording finding is the better-evidenced one: it cites the single
        run it examined, which the composer requires of an ``unsupported_claim`` finding. A
        claim that is overconfident but not universal is left to RULE 3.
        """

        verification = [
            item for item in context.linked_evidence if item.kind in _VERIFICATION_KINDS
        ]
        if not verification:
            return None
        if len(verification) > 1:
            return None
        if not _asserts_universal(claim):
            return None

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

    def _overconfidence(self, claim: Claim, context: CriticContext) -> CriticResult | None:
        """RULE 3: a stated confidence above the ceiling its backing strength can carry.

        Returns ``None`` when the claim states no number at all, or when its number is within
        the ceiling. A claim that asserts nothing numeric is not assessed here — there is
        nothing to compare, and inventing a number would manufacture a finding.
        """

        stated = _stated_confidence(claim, context)
        if stated is None:
            return None

        strength = _backing_strength(claim, context)
        ceiling = _CONFIDENCE_CEILING[strength]
        if stated <= ceiling:
            return None

        observations = [
            f'Claim {claim.id} states a confidence of {stated}%: "{claim.statement}"',
            f"The strongest support linked to the claim is {strength.value}, whose ceiling is "
            f"{ceiling}%.",
        ]
        limitations = [
            "The ceilings are a declared policy, not a measurement of this agent's real "
            "calibration: per-agent calibration needs a persisted history of past claims and "
            "their outcomes, which this critic does not hold.",
            "Evidence strength is read as `self_reported` for every stored evidence item, "
            "because evidence is what a producer reported; only an observation NYX gathered "
            "itself can be stronger. A linked record is therefore never treated as "
            "verification of the current commit.",
            "This is an assessment of proportion between a stated number and its support, not "
            "a verification that the claim is true or false.",
        ]
        # An `unsupported_claim` finding must cite the evidence it examined, or the composer
        # refuses it. With nothing linked there is nothing to cite, so the finding is recorded
        # as a scoped verification gap instead -- the same split C-001 makes, and the same one
        # that keeps every finding this critic can produce composable.
        examined = [item.id for item in context.linked_evidence]
        category = (
            FindingCategory.UNSUPPORTED_CLAIM if examined else FindingCategory.MISSING_VERIFICATION
        )

        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"Claim {claim.id} states {stated}% confidence, but its linked support "
                f"({strength.value}) cannot honestly carry more than {ceiling}%."
            ),
            scope=claim.scope,
            observations=observations,
            inferences=[
                "The claim's stated certainty outruns what is linked behind it, so the number "
                "should not be relied on as a calibrated probability.",
            ],
            limitations=limitations,
            rationale=(
                f"Claim {claim.id} states {stated}% confidence. The strongest support linked "
                f"to it is {strength.value}, which this critic's declared ceiling caps at "
                f"{ceiling}%. The stated number exceeds the ceiling, so the certainty is "
                "unsupported by the evidence behind it."
            ),
            recommended_next_step=(
                "Either lower the stated confidence to what the linked support can carry, or "
                "attach stronger verification — an observation NYX gathered itself — before "
                "asserting a number this high."
            ),
            category=category,
            severity=Severity.MEDIUM,
            title=f"Stated confidence exceeds its support in claim {claim.id}",
            summary=(
                f"Claim {claim.id} states {stated}% confidence against "
                f"{strength.value} support, whose ceiling is {ceiling}%."
            ),
            supporting_evidence_ids=examined,
            claim_ids=[claim.id],
            event_ids=[context.target_event.id],
            affected_system_ids=[claim.claimant_system_id],
        )
