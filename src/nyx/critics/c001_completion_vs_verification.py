"""C-001 — Completion claim vs. verification (SPEC §8).

Compares a completion claim ("done", "fixed", "tests pass", "deployed") against the linked
verification artifact. Fires when the artifact is missing, stale, failed, or refers to a
different commit/build/version than the claim.

**Verification strength.** A completion claim now needs support of at least
``rerun_by_nyx`` strength, and the claiming agent's own report never counts as verification. The
evidence-linked path below is unchanged and still works; the strength path is additive and gated
by ``require_independent_verification``. With the flag off this critic behaves exactly as it did
before the ground-truth layer existed, and every existing test keeps passing.

Declared scope: completion claims that name a commit, and the test/CI evidence and probe
observations linked to them. Missing test evidence produces a **scoped verification gap** — it is
never an assertion that the code is broken (SPEC §3.2 invariant 4: missing telemetry is unknown,
not misconduct).

Limitation: the evidence model has no structured test-outcome field, so the recorded outcome is
read from the evidence title/description with explicit word-boundary markers. That is deterministic
but weaker than a structured result; it is documented here rather than hidden. The strength path
does not share the limitation: an observation records its own result.
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
    Reproducibility,
    Severity,
    VerificationStrength,
)
from nyx.domain.evidence import EvidenceItem
from nyx.domain.observation import Observation
from nyx.evidence.verification import COMPLETION_MINIMUM_STRENGTH, is_at_least

CRITIC_ID = "c001-completion-vs-verification"
CRITIC_VERSION = "1.0.0"

_VERIFICATION_KINDS = {EvidenceKind.TEST_RESULT, EvidenceKind.CI_TEST_RESULT}

_SUCCESS_MARKER = re.compile(r"\bpass(?:es|ed|ing)?\b|\bgreen\b")
_FAILURE_MARKER = re.compile(r"\bfail(?:s|ed|ing|ure)?\b|\bbroken\b|\bred\b")


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
    """Assess completion claims against their linked verification artifact.

    ``require_strength`` switches on the stronger rule. It is a constructor argument rather than a
    module constant so a deployment can adopt the ground-truth check without a code change, and so
    the off-state is testable without monkeypatching.
    """

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Detect completion claims whose linked verification artifact is missing, stale, "
        "failed, or targets a different commit/build/version; when strength checking is enabled, "
        "require support of at least rerun_by_nyx strength and reject self-reported evidence."
    )
    scope = CriticScope(
        can_assess=[
            "completion claims that name a commit",
            "linked test_result and ci_test_result evidence for the claimed commit",
            "probe observations linked to the claim, and their verification strength",
        ],
        cannot_assess=[
            "completion claims that do not name a commit",
            "code correctness when no verification evidence is linked",
            "deployment or production state",
            "whether the claimant's report is *honest*, as opposed to unverified",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def __init__(self, *, require_strength: bool = False) -> None:
        self.require_strength = require_strength

    def _run_strength_path(self, context: CriticContext, claim: Claim, commit: str) -> CriticResult:
        """The stronger rule: a completion claim needs support independent of the claimant.

        Three outcomes, in the order they are decided:

        1. an observation of at least ``rerun_by_nyx`` strength covering the claimed commit
           *passes* -- and the achieved strength is stated either way;
        2. observations exist but none covers the claimed commit, or none is strong enough --
           a **verification gap** naming the strength achieved;
        3. nothing independent exists at all, and the only linked support is the claimant's own
           report -- the case the brief names, where the claiming agent's report is the whole of
           the evidence.

        Evidence never raises the achieved strength. A linked CI record is ``self_reported``
        whatever its integrity method, so it can explain a claim but cannot verify one.
        """

        observations = _observations_for(context, claim.id)
        achieved, best = _strongest_support(observations, commit)

        if best is not None and is_at_least(achieved, COMPLETION_MINIMUM_STRENGTH):
            return _strength_pass_result(
                claim=claim,
                context=context,
                achieved=achieved,
                observation=best,
                scope=context.scope,
            )

        covering = [item for item in observations if _covers_version(item, commit)]
        self_reported = _self_reported_evidence(claim, context)

        if not covering:
            if observations:
                return _strength_result(
                    proposition=(
                        f"Completion claim {claim.id} for commit {commit} has observations, but "
                        f"none covers that commit (strongest achieved: {achieved.value})."
                    ),
                    scope=context.scope,
                    observations=[
                        f"Claim {claim.id} asserts completion for commit {commit}.",
                        f"{len(observations)} observation(s) are linked to the claim.",
                        "No linked observation declares the claimed commit as its subject version.",
                    ],
                    limitations=[
                        "An undated observation cannot verify a dated claim; this is a gap, not "
                        "a defect assertion.",
                        "A verification run may exist outside NYX; absent telemetry is unknown, "
                        "not misconduct.",
                    ],
                    rationale=(
                        f"The linked observations do not name commit {commit}, so none of them "
                        "can verify the claim as stated. The strongest support achieved is "
                        f"{achieved.value}, below the {COMPLETION_MINIMUM_STRENGTH.value} floor."
                    ),
                    next_step=(
                        f"Run a probe against commit {commit} and link the resulting observation "
                        "to this claim."
                    ),
                    claim=claim,
                    context=context,
                    achieved=achieved,
                    subject_version=commit,
                    observation_ids=[item.id for item in observations],
                    title=f"No observation covers completion claim on {commit}",
                )

            if self_reported:
                evidence = self_reported[0]
                producer = (
                    evidence.producer_identity.id if evidence.producer_identity else "unknown"
                )
                return _strength_result(
                    proposition=(
                        f"Completion claim {claim.id} for commit {commit} is supported only by "
                        f"the claimant's own report ({evidence.id}); a claimant cannot verify "
                        "itself."
                    ),
                    scope=context.scope,
                    observations=[
                        f"Claim {claim.id} asserts completion for commit {commit}.",
                        f"The only linked verification support is evidence {evidence.id}, "
                        f"whose producer is {producer}.",
                        "No observation gathered by NYX is linked to the claim.",
                    ],
                    limitations=[
                        "This is a verification gap, not an assertion that the work is defective.",
                        "The claimant's report may be accurate; unverified is not the same as "
                        "false.",
                        "A verification run may exist outside NYX; absent telemetry is unknown, "
                        "not misconduct.",
                    ],
                    rationale=(
                        f"The only linked verification support for commit {commit} was produced "
                        "by the claimant itself. Independent verification is what the claim needs "
                        "and does not have, so the achieved strength is "
                        f"{VerificationStrength.SELF_REPORTED.value}."
                    ),
                    next_step=(
                        f"Re-run the required suite against commit {commit} from NYX and link "
                        "the observation, or have an independent party verify it."
                    ),
                    claim=claim,
                    context=context,
                    achieved=VerificationStrength.SELF_REPORTED,
                    subject_version=commit,
                    severity=Severity.HIGH,
                    title="Completion claim rests only on the claimant's own report",
                )

            return _strength_result(
                proposition=(
                    f"Completion claim {claim.id} for commit {commit} has no independent "
                    "verification at all (strength achieved: none)."
                ),
                scope=context.scope,
                observations=[
                    f"Claim {claim.id} asserts completion for commit {commit}.",
                    "No observation gathered by NYX is linked to the claim.",
                ],
                limitations=[
                    "This is a verification gap, not a defect assertion.",
                    "A verification run may exist outside NYX; absent telemetry is unknown, not "
                    "misconduct.",
                ],
                rationale=(
                    f"Nothing independent of the claimant backs commit {commit}. The achieved "
                    "strength is none, below the "
                    f"{COMPLETION_MINIMUM_STRENGTH.value} floor."
                ),
                next_step=(
                    f"Run a probe against commit {commit} and link the observation to this claim."
                ),
                claim=claim,
                context=context,
                achieved=VerificationStrength.NONE,
                subject_version=commit,
            )

        return _strength_result(
            proposition=(
                f"Completion claim {claim.id} for commit {commit} has covering observations, but "
                f"the strongest is only {achieved.value}, below the "
                f"{COMPLETION_MINIMUM_STRENGTH.value} floor."
            ),
            scope=context.scope,
            observations=[
                f"Claim {claim.id} asserts completion for commit {commit}.",
                f"Observation {covering[0].id} covers the claimed commit at "
                f"{covering[0].verification_strength.value} strength.",
            ],
            limitations=[
                "The observation is real but weaker than the floor; it does not establish what "
                "the claim asserts.",
            ],
            rationale=(
                f"An observation covers commit {commit}, but at {achieved.value} it does not "
                f"reach {COMPLETION_MINIMUM_STRENGTH.value}, so it cannot verify the claim."
            ),
            next_step=(
                "Re-run the check from NYX at the current subject version so the observation "
                "reaches rerun_by_nyx strength or above."
            ),
            claim=claim,
            context=context,
            achieved=achieved,
            subject_version=commit,
            observation_ids=[item.id for item in covering],
        )

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

        if self.require_strength:
            return self._run_strength_path(context, claim, commit)

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


# --- the strength path -----------------------------------------------------
#
# Kept apart from the evidence path rather than interleaved with it, because the two answer
# different questions. The evidence path asks "is there a covering artifact and does it pass?".
# The strength path asks "is anything backing this claim independent of the claimant, and how
# strong is the strongest such thing?". A claim can satisfy the first and fail the second, and
# that is the case the brief's acceptance criterion names.


def _observation_strength(observation: Observation) -> VerificationStrength:
    """The strength of one observation, defaulting to the probe kind's own default."""

    return observation.verification_strength


def _observations_for(context: CriticContext, claim_id: str) -> list[Observation]:
    return [item for item in context.observations if item.relevance_to_claim == claim_id]


def _covers_version(observation: Observation, commit: str) -> bool:
    """An observation covers the claimed commit only when it declares that version.

    Unlike evidence, an observation with no declared ``subject_version`` never covers a dated
    claim: an undated observation cannot verify a dated one, and guessing would turn "NYX looked
    at something once" into verification of this commit.
    """

    return observation.subject_version == commit


def _strongest_support(
    observations: list[Observation], commit: str
) -> tuple[VerificationStrength, Observation | None]:
    """Return the strongest observation covering ``commit``, or ``none`` with no observation."""

    covering = [item for item in observations if _covers_version(item, commit)]
    if not covering:
        return VerificationStrength.NONE, None
    best = max(covering, key=lambda item: _STRENGTH_ORDER[_observation_strength(item)])
    return _observation_strength(best), best


_STRENGTH_ORDER: dict[VerificationStrength, int] = {
    strength: index for index, strength in enumerate(VerificationStrength)
}


def _self_reported_evidence(claim: Claim, context: CriticContext) -> list[EvidenceItem]:
    """Evidence linked to the claim that the claimant itself produced.

    A claimant cannot verify itself (ADR 0005). This is the specific record of *why* the strength
    path refused a claim that had linked evidence: not "no evidence", but "only your own".
    """

    claimant = claim.claimant_system_id
    return [
        item
        for item in context.linked_evidence
        if item.relevance_to_claim == claim.id
        and item.producer_identity is not None
        and item.producer_identity.id == claimant
    ]


def _strength_result(
    *,
    proposition: str,
    scope: Scope,
    observations: list[str],
    limitations: list[str],
    rationale: str,
    next_step: str,
    claim: Claim,
    context: CriticContext,
    achieved: VerificationStrength,
    subject_version: str,
    observation_ids: list[str] | None = None,
    severity: Severity = Severity.MEDIUM,
    title: str = "Completion claim is not independently verified",
) -> CriticResult:
    """Build a strength-path result.

    The achieved strength is emitted on every branch, including the passing one, because "what
    strength did this claim reach" is a fact a reader needs either way -- and a finding that omits
    it would make the failure look like a gap in the record rather than in the claim.
    """

    return CriticResult(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        result=CriticResultStatus.FINDING,
        proposition=proposition,
        scope=scope,
        observations=[*observations, f"Verification strength achieved: {achieved.value}."],
        limitations=limitations,
        rationale=rationale,
        recommended_next_step=next_step,
        category=FindingCategory.MISSING_VERIFICATION,
        severity=severity,
        title=title,
        summary=proposition,
        claim_ids=[claim.id],
        event_ids=[context.target_event.id],
        affected_system_ids=[claim.claimant_system_id],
        observation_refs=list(observation_ids or []),
        subject_version=subject_version,
        reproducibility=Reproducibility.UNREPRODUCED,
        verification_strength=achieved,
        remediation_hint=(
            "Link an observation NYX gathered itself for this subject version, or re-run the "
            "check and record the result as an observation."
        ),
    )


def _strength_pass_result(
    *,
    claim: Claim,
    context: CriticContext,
    achieved: VerificationStrength,
    observation: Observation,
    scope: Scope,
) -> CriticResult:
    """Build the strength-path ``no_finding`` result, stating what was achieved."""

    return CriticResult(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        result=CriticResultStatus.NO_FINDING,
        proposition=(
            f"Completion claim {claim.id} is supported by an independent observation of "
            f"{achieved.value} strength."
        ),
        scope=scope,
        observations=[
            f"Observation {observation.id} covers the claimed commit and records "
            f"{observation.result.value}.",
            f"Verification strength achieved: {achieved.value}.",
        ],
        rationale=(
            f"Observation {observation.id} was gathered by probe {observation.probe_id} at "
            f"version {observation.subject_version}, independently of the claimant, and records "
            f"{observation.detail or 'a result'}. The claim therefore clears the "
            f"{COMPLETION_MINIMUM_STRENGTH.value} floor. This means the claim is supported at "
            "that strength; it is not a certification that the underlying work is correct."
        ),
        recommended_next_step="No action required from this critic.",
        claim_ids=[claim.id],
        event_ids=[context.target_event.id],
        affected_system_ids=[claim.claimant_system_id],
        observation_refs=[observation.id],
        subject_version=observation.subject_version,
        verification_strength=achieved,
    )
