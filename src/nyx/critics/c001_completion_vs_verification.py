"""C-001 — Completion claim vs. verification (SPEC §8).

Compares a completion claim ("done", "fixed", "tests pass", "deployed") against the linked
verification artifact. Fires when the artifact is missing, stale, failed, or refers to a
different commit/build/version than the claim.

Declared scope: completion claims that name a commit, and the test/CI evidence linked to
them. Missing test evidence produces a **scoped verification gap** — it is never an
assertion that the code is broken (SPEC §3.2 invariant 4: missing telemetry is unknown, not
misconduct).

Limitation: the evidence model has no structured test-outcome field, so the recorded
outcome is read from the evidence title/description with explicit word-boundary markers.
That is deterministic but weaker than a structured result; it is documented here rather
than hidden.
"""

from __future__ import annotations

import re

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimType,
    CriticResultStatus,
    CriticType,
    EvidenceKind,
    FindingCategory,
    Severity,
)
from nyx.domain.evidence import EvidenceItem

CRITIC_ID = "c001-completion-vs-verification"
CRITIC_VERSION = "1.0.0"

_VERIFICATION_KINDS = {EvidenceKind.TEST_RESULT, EvidenceKind.CI_TEST_RESULT}

_SUCCESS_MARKER = re.compile(r"\bpass(?:ed|ing)?\b|\bgreen\b")
_FAILURE_MARKER = re.compile(r"\bfail(?:ed|ing|ure)?\b|\bbroken\b|\bred\b")


def _recorded_outcome(evidence: EvidenceItem) -> str | None:
    """Read the recorded test outcome from the evidence title and description."""

    text = f"{evidence.title} {evidence.description}".lower()
    if _FAILURE_MARKER.search(text):
        return "failed"
    if _SUCCESS_MARKER.search(text):
        return "passed"
    return None


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no issue within its declared scope. It
    is never a statement that the claim is true, that the code is correct, or that no other
    critic could find something.
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


class CompletionVsVerificationCritic:
    """Assess completion claims against their linked verification artifact."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Detect completion claims whose linked verification artifact is missing, stale, "
        "failed, or targets a different commit/build/version."
    )
    scope = CriticScope(
        can_assess=[
            "completion claims that name a commit",
            "linked test_result and ci_test_result evidence for the claimed commit",
        ],
        cannot_assess=[
            "completion claims that do not name a commit",
            "code correctness when no verification evidence is linked",
            "deployment or production state",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def run(self, context: CriticContext) -> CriticResult:
        claim = context.linked_claim
        if claim is None or claim.claim_type is not ClaimType.COMPLETION:
            return _no_finding(
                "No completion claim is linked to this event.",
                "This critic assesses completion claims; the linked record is not a completion "
                "claim, so there is nothing to assess within this critic's declared scope.",
                context.scope,
            )

        commit = claim.scope.commit
        if not commit:
            return _no_finding(
                f"Completion claim {claim.id} does not name a commit.",
                "This critic compares a completion claim against verification for the commit the "
                "claim names; without a commit there is no version to compare, so this critic "
                "finds nothing within its declared scope.",
                context.scope,
            )

        verification = [
            item for item in context.linked_evidence if item.kind in _VERIFICATION_KINDS
        ]
        if not verification:
            return CriticResult(
                critic_id=CRITIC_ID,
                critic_version=CRITIC_VERSION,
                result=CriticResultStatus.FINDING,
                proposition=(
                    f"Completion claim {claim.id} for commit {commit} has no linked "
                    "verification artifact."
                ),
                scope=claim.scope,
                observations=[
                    f"Claim {claim.id} asserts completion for commit {commit}.",
                    "No test_result or ci_test_result evidence is linked to the claim or event.",
                ],
                limitations=[
                    "This is a verification gap: the claim is unverified, not verified false. It "
                    "is not an assertion that the code is defective.",
                    "A passing result may exist outside NYX; absent telemetry is unknown, not "
                    "misconduct.",
                ],
                rationale=(
                    f"The claim names commit {commit}, but no verification artifact is linked, "
                    "so the claim is currently unverifiable. This is a scoped verification gap "
                    "for commit "
                    f"{commit}, not an accusation that the implementation is broken."
                ),
                recommended_next_step=(
                    f"Run the required suite against commit {commit} and link the result as "
                    "evidence."
                ),
                category=FindingCategory.MISSING_VERIFICATION,
                severity=Severity.MEDIUM,
                title=f"Verification gap: no test evidence for completion claim on {commit}",
                summary=(
                    f"Completion claim {claim.id} names commit {commit} but no verification "
                    "artifact is linked. The claim is unverified; this is a gap, not a defect "
                    "assertion."
                ),
                claim_ids=[claim.id],
                event_ids=[context.target_event.id],
                affected_system_ids=[claim.claimant_system_id],
            )

        covering = [item for item in verification if _covers(item, commit)]
        failed = [item for item in covering if _recorded_outcome(item) == "failed"]
        if failed:
            evidence = failed[0]
            return CriticResult(
                critic_id=CRITIC_ID,
                critic_version=CRITIC_VERSION,
                result=CriticResultStatus.FINDING,
                proposition=(
                    f"Completion claim {claim.id} for commit {commit} is contradicted by a "
                    f"failed verification artifact ({evidence.id})."
                ),
                scope=claim.scope,
                observations=[
                    f"Claim {claim.id} asserts completion for commit {commit}.",
                    f"Linked evidence {evidence.id} records a failed outcome for commit "
                    f"{_evidence_commit(evidence)}.",
                ],
                counterevidence_ids=[evidence.id],
                rationale=(
                    f"The linked verification artifact {evidence.id} covers commit {commit} "
                    "and records a failed outcome, which contradicts the completion claim."
                ),
                recommended_next_step=(
                    f"Investigate the failed verification for commit {commit} before treating "
                    "the claim as satisfied."
                ),
                category=FindingCategory.UNSUPPORTED_CLAIM,
                severity=Severity.HIGH,
                title=f"Failed verification for completion claim on {commit}",
                summary=(
                    f"Evidence {evidence.id} records a failed verification for commit "
                    f"{commit}, contradicting completion claim {claim.id}."
                ),
                claim_ids=[claim.id],
                event_ids=[context.target_event.id],
                affected_system_ids=[claim.claimant_system_id],
            )

        fresh_pass = [
            item
            for item in covering
            if _recorded_outcome(item) == "passed" and not _is_stale(item, claim)
        ]
        if fresh_pass:
            return _no_finding(
                f"Completion claim {claim.id} is verified for commit {commit}.",
                f"Linked evidence {fresh_pass[0].id} covers commit {commit} with a passing "
                "recorded outcome and a validity window that covers the claim. This critic "
                "found no issue within its declared scope; it does not certify the claim "
                "beyond the linked evidence.",
                context.scope,
            )

        stale = [item for item in covering if _is_stale(item, claim)]
        if stale:
            evidence = stale[0]
            return CriticResult(
                critic_id=CRITIC_ID,
                critic_version=CRITIC_VERSION,
                result=CriticResultStatus.FINDING,
                proposition=(
                    f"Completion claim {claim.id} for commit {commit} is only supported by "
                    f"stale verification evidence ({evidence.id})."
                ),
                scope=claim.scope,
                observations=[
                    f"Claim {claim.id} asserts completion for commit {commit}.",
                    f"Linked evidence {evidence.id} expired at {evidence.valid_until}, before "
                    f"the claim was made at {claim.created_at}.",
                ],
                limitations=[
                    "A stale artifact cannot verify a newer claim; it is not evidence that the "
                    "code is defective.",
                ],
                rationale=(
                    f"The only covering verification artifact {evidence.id} expired before the "
                    "claim was made, so it cannot verify the claim as stated."
                ),
                recommended_next_step=(
                    f"Re-run the required suite for commit {commit} and link a current result."
                ),
                category=FindingCategory.UNSUPPORTED_CLAIM,
                severity=Severity.MEDIUM,
                title=f"Stale verification for completion claim on {commit}",
                summary=(
                    f"Evidence {evidence.id} for commit {commit} expired before completion "
                    f"claim {claim.id} was made and cannot verify it."
                ),
                supporting_evidence_ids=[evidence.id],
                claim_ids=[claim.id],
                event_ids=[context.target_event.id],
                affected_system_ids=[claim.claimant_system_id],
            )

        mismatched = [item for item in verification if not _covers(item, commit)]
        unreadable = [item for item in covering if _recorded_outcome(item) is None]
        if mismatched:
            evidence = mismatched[0]
            return CriticResult(
                critic_id=CRITIC_ID,
                critic_version=CRITIC_VERSION,
                result=CriticResultStatus.FINDING,
                proposition=(
                    f"Completion claim {claim.id} names commit {commit}, but the linked "
                    f"verification artifact ({evidence.id}) targets commit "
                    f"{_evidence_commit(evidence)}."
                ),
                scope=claim.scope,
                observations=[
                    f"Claim {claim.id} asserts completion for commit {commit}.",
                    f"Linked evidence {evidence.id} records a verification run for commit "
                    f"{_evidence_commit(evidence)}.",
                ],
                limitations=[
                    "The mismatched artifact says nothing about commit "
                    f"{commit}; this is not an assertion that the code is defective.",
                ],
                rationale=(
                    f"The completion claim names commit {commit}, but the attached verification "
                    f"run {evidence.id} targets {_evidence_commit(evidence)}, so the artifact "
                    "does not verify the stated commit."
                ),
                recommended_next_step=(
                    f"Run the required suite against commit {commit} and link the result as "
                    "evidence."
                ),
                category=FindingCategory.UNSUPPORTED_CLAIM,
                severity=Severity.HIGH,
                title=(
                    "Verification artifact targets a different commit "
                    f"({_evidence_commit(evidence)})"
                ),
                summary=(
                    f"Completion claim {claim.id} names commit {commit}, but evidence "
                    f"{evidence.id} covers {_evidence_commit(evidence)}."
                ),
                supporting_evidence_ids=[evidence.id],
                claim_ids=[claim.id],
                event_ids=[context.target_event.id],
                affected_system_ids=[claim.claimant_system_id],
            )

        evidence = unreadable[0]
        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"Completion claim {claim.id} for commit {commit} is linked to a verification "
                f"artifact ({evidence.id}) whose recorded outcome cannot be read."
            ),
            scope=claim.scope,
            observations=[
                f"Claim {claim.id} asserts completion for commit {commit}.",
                f"Linked evidence {evidence.id} covers commit {commit} but does not record a "
                "readable outcome.",
            ],
            limitations=[
                "The claim is unverified, not verified false; this is a verification gap.",
            ],
            rationale=(
                f"Evidence {evidence.id} covers the claimed commit but does not record a "
                "machine-readable outcome, so the claim cannot be verified from it."
            ),
            recommended_next_step=(
                "Link a verification artifact that records an explicit outcome for "
                f"commit {commit}."
            ),
            category=FindingCategory.MISSING_VERIFICATION,
            severity=Severity.MEDIUM,
            title=f"Verification artifact for {commit} has no readable outcome",
            summary=(
                f"Evidence {evidence.id} covers commit {claim.scope.commit} but records no "
                f"readable outcome, leaving completion claim {claim.id} unverified."
            ),
            supporting_evidence_ids=[evidence.id],
            claim_ids=[claim.id],
            event_ids=[context.target_event.id],
            affected_system_ids=[claim.claimant_system_id],
        )


def _covers(evidence: EvidenceItem, commit: str) -> bool:
    """Return whether the evidence's declared scope covers the claimed commit."""

    if evidence.scope is None or not evidence.scope.commit:
        return False
    return evidence.scope.commit == commit


def _evidence_commit(evidence: EvidenceItem) -> str:
    if evidence.scope is not None and evidence.scope.commit:
        return evidence.scope.commit
    return "<unknown>"


def _is_stale(evidence: EvidenceItem, claim: Claim) -> bool:
    if evidence.valid_until is None:
        return False
    return evidence.valid_until < claim.created_at
