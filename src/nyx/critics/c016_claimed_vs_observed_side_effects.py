"""C-016 — Claimed vs. observed side effects (SPEC §8).

Rule: the claimed effect of an action does not match the observed end state.

An agent says it saved a booking, wrote a row, queued a message, or deleted a record. NYX can
check that independently: an observation gathered by a probe records what the end state actually
was. This critic compares the two. It fires when a side effect is *claimed* — detected by explicit
token matching on the claim statement and on the descriptions of the evidence linked to the claim
— and no observation confirms it: either no observation is linked at all, the linked observation
records ``NOT_OBSERVED``, or the observation's ``subject_version`` is not the commit the claim is
scoped to.

Declared scope: claims that assert a side effect, the observations NYX gathered against them, and
the evidence linked to them. Severity is HIGH when the claimed effect is entirely absent — no
observation linked at all, or an observation that looked at the claimed version and found nothing
(a booking that was never written) — and MEDIUM when an observation exists but does not match (a
different subject version, or a probe that errored or was skipped).

Limitation: this critic cannot assess whether the effect was produced by a *different* path than
the one claimed. An observation that confirms the row exists confirms the end state, not that
this action caused it; an observation that finds nothing says the effect is absent from the
observed state, not that the action failed. Detection is token matching, so a claim that asserts
an effect in wording outside the token set is not seen at all, and prose that merely mentions a
write without asserting one is treated as a claim only when the token matches. The critic is a
pure function over :class:`~nyx.critics.context.CriticContext`: it reads no storage and writes
nothing, and a ``no_finding`` result is never proof that the claimed effect happened.
"""

from __future__ import annotations

import hashlib
import re

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Digest, Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    FindingCategory,
    ProbeOutcome,
    Reproducibility,
    Severity,
    VerificationStrength,
)
from nyx.domain.evidence import EvidenceItem
from nyx.domain.observation import Observation

CRITIC_ID = "c016-claimed-vs-observed-side-effects"
CRITIC_VERSION = "1.0.0"

#: Explicit side-effect verbs. Word-bounded, so "sender" is not a "send" and "restored" is not a
#: "store". Deliberately a closed set: a claim that asserts an effect in other wording is out of
#: this critic's declared scope rather than guessed at.
_SIDE_EFFECT_TOKEN = re.compile(
    r"\b(?:"
    r"save[sd]?|saving|"
    r"create[sd]?|creating|"
    r"write|writes|wrote|written|writing|"
    r"insert(?:s|ed|ing)?|"
    r"queu(?:e|es|ed|ing)|"
    r"send|sends|sent|sending|"
    r"update[sd]?|updating|"
    r"delete[sd]?|deleting|"
    r"push(?:es|ed|ing)?|"
    r"persist(?:s|ed|ing)?|"
    r"store[sd]?|storing|"
    r"append(?:s|ed|ing)?|"
    r"publish(?:es|ed|ing)?|"
    r"emit(?:s|ted|ting)?|"
    r"dispatch(?:es|ed|ing)?"
    r")\b"
)


def _no_finding(
    proposition: str,
    rationale: str,
    scope: Scope,
    observations: list[str] | None = None,
) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no issue within its declared scope. It is
    never a statement that the claim is true, that the claimed effect happened, or that no other
    critic could find something.
    """

    return CriticResult(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        result=CriticResultStatus.NO_FINDING,
        proposition=proposition,
        scope=scope,
        observations=observations or ["No issue found within this critic's declared scope."],
        rationale=rationale,
        recommended_next_step="No action required from this critic.",
    )


def _claims_side_effect(claim: Claim, evidence: list[EvidenceItem]) -> bool:
    """RULE: does the claim, or the evidence linked to it, assert a side effect?

    Both sources are read because a producer may assert the effect in its own report rather than
    in the claim statement. The match is an explicit token test, never a reading of intent: if no
    side-effect verb appears, nothing is claimed and there is nothing to assess.
    """

    if _SIDE_EFFECT_TOKEN.search(claim.statement.lower()):
        return True
    return any(_SIDE_EFFECT_TOKEN.search(item.description.lower()) for item in evidence)


def _observations_for(context: CriticContext, claim_id: str) -> list[Observation]:
    """Return the observations NYX gathered against this claim."""

    return [item for item in context.observations if item.relevance_to_claim == claim_id]


def _confirms(observation: Observation, commit: str) -> bool:
    """An observation confirms a claimed effect only at the claimed version, with a positive result.

    An undated observation never confirms a dated claim, and an observation of another commit says
    nothing about this one. Guessing would turn "NYX looked at something once" into confirmation
    of an effect this action is supposed to have produced.
    """

    return observation.result is ProbeOutcome.OBSERVED and observation.subject_version == commit


def _root_cause_key(claim: Claim) -> Digest:
    """A stable defect identity for one claim's claimed effect, so repeats dedupe to one finding."""

    payload = f"{CRITIC_ID}|{claim.id}|{claim.scope.commit or ''}".encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _finding_result(
    *,
    context: CriticContext,
    claim: Claim,
    commit: str,
    observations: list[Observation],
    severity: Severity,
    title: str,
    proposition: str,
    rationale: str,
    next_step: str,
    limitations: list[str],
) -> CriticResult:
    """Build the finding for a claimed side effect that no observation confirms.

    The category follows the composer's evidence-reference rule: an ``unsupported_claim`` finding
    must cite at least one evidence item it examined, so when nothing is linked the finding is
    recorded as a scoped verification gap instead. That is the same split C-001 makes, and it
    keeps every finding this critic can produce composable rather than raising inside the pack.
    """

    examined = [item.id for item in context.linked_evidence]
    category = (
        FindingCategory.UNSUPPORTED_CLAIM if examined else FindingCategory.MISSING_VERIFICATION
    )
    return CriticResult(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        result=CriticResultStatus.FINDING,
        proposition=proposition,
        scope=claim.scope,
        observations=[
            f'Claim {claim.id} states: "{claim.statement}"',
            f"The claim is scoped to commit {commit}.",
            (
                f"Observation {observations[0].id} is linked to the claim but does not confirm "
                "the claimed effect."
                if observations
                else "No observation gathered by NYX is linked to the claim."
            ),
            f"Verification strength achieved: {VerificationStrength.NONE.value}.",
        ],
        limitations=limitations,
        rationale=rationale,
        recommended_next_step=next_step,
        requires_human_review=True,
        category=category,
        severity=severity,
        title=title,
        summary=proposition,
        claim_ids=[claim.id],
        event_ids=[context.target_event.id],
        affected_system_ids=[claim.claimant_system_id],
        supporting_evidence_ids=examined,
        observation_refs=[item.id for item in observations],
        root_cause_key=_root_cause_key(claim),
        subject_version=commit,
        reproducibility=Reproducibility.UNREPRODUCED,
        verification_strength=VerificationStrength.NONE,
        remediation_hint=(
            "Re-run the action's effect as a probe against this subject version and link the "
            "observation to the claim."
        ),
    )


class ClaimedVsObservedSideEffectsCritic:
    """Assess a claimed side effect against the end state NYX actually observed."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Detect claims that assert a side effect (save/create/write/insert/queue/send/update/"
        "delete) which no linked observation confirms — either because no observation is linked, "
        "because the observation records not_observed, or because it targets a different commit."
    )
    scope = CriticScope(
        can_assess=[
            "claims that assert a side effect, by explicit token match on the claim statement "
            "and on linked evidence descriptions",
            "observations NYX gathered against those claims, and their result and subject version",
            "evidence linked to those claims, and whether it cites the same side effect",
        ],
        cannot_assess=[
            "whether the effect was produced by a different path than the one claimed",
            "claims that do not assert a side effect",
            "claims that do not name a commit",
            "the truth of the claim: a confirming observation confirms the end state, not "
            "the cause",
            "effects asserted in wording outside the explicit side-effect token set",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def run(self, context: CriticContext) -> CriticResult:
        claim = context.linked_claim
        if claim is None:
            return _no_finding(
                "No claim is linked to this event.",
                "This critic compares a claimed side effect against the observed end state; "
                "without a linked claim there is nothing to assess within this critic's declared "
                "scope.",
                context.scope,
                ["No claim is linked to the target event."],
            )

        commit = claim.scope.commit
        if not commit:
            return _no_finding(
                f"Claim {claim.id} does not name a commit.",
                "This critic binds an observation to the commit the claim is scoped to; without "
                "a commit there is no version to compare, so this critic finds nothing within "
                "its declared scope.",
                context.scope,
                [f"Claim {claim.id} is scoped to {claim.scope.system_id} with no commit."],
            )

        if not _claims_side_effect(claim, context.linked_evidence):
            return _no_finding(
                f"Claim {claim.id} does not assert a side effect.",
                "No side-effect verb appears in the claim statement or in the descriptions of "
                "the evidence linked to it, so there is no claimed effect to compare against an "
                "observation. This critic found nothing within its declared scope; it is not a "
                "statement that the claim is true.",
                context.scope,
                [
                    f'Claim {claim.id} states: "{claim.statement}"',
                    "No side-effect token matched the claim statement or its linked evidence.",
                ],
            )

        observations = _observations_for(context, claim.id)
        confirming = [item for item in observations if _confirms(item, commit)]
        if confirming:
            return _confirmed_result(context, claim, commit, confirming[0])

        return self._unconfirmed_result(context, claim, commit, observations)

    def _unconfirmed_result(
        self,
        context: CriticContext,
        claim: Claim,
        commit: str,
        observations: list[Observation],
    ) -> CriticResult:
        """Build the finding for a claimed effect no observation confirms.

        Three distinct gaps, in the order they are decided: nothing was linked at all; something
        was linked and it looked at the claimed version and found nothing (the effect is absent
        from the observed end state); and something was linked but does not cover the claimed
        version, or did not complete.
        """

        not_observed = [
            item
            for item in observations
            if item.result is ProbeOutcome.NOT_OBSERVED and item.subject_version == commit
        ]
        mismatched = [item for item in observations if item.subject_version != commit]

        if not observations:
            return _finding_result(
                context=context,
                claim=claim,
                commit=commit,
                observations=observations,
                severity=Severity.HIGH,
                title=f"Claimed side effect has no observation for {commit}",
                proposition=(
                    f"Claim {claim.id} asserts a side effect for commit {commit}, but no "
                    "observation gathered by NYX is linked to the claim."
                ),
                rationale=(
                    f"Claim {claim.id} asserts a side effect, and nothing NYX observed is linked "
                    f"to it for commit {commit}. The claimed effect is unconfirmed: this is a gap "
                    "in the record, not an assertion that the action failed."
                ),
                next_step=(
                    f"Run a probe against commit {commit} and link the resulting observation to "
                    "this claim."
                ),
                limitations=[
                    "This is a gap in the record, not proof that the action failed.",
                    "The effect may have been produced by a different path than the one claimed; "
                    "this critic cannot assess that.",
                    "A probe may exist outside NYX; absent telemetry is unknown, not misconduct.",
                ],
            )

        if not_observed:
            return _finding_result(
                context=context,
                claim=claim,
                commit=commit,
                observations=observations,
                severity=Severity.HIGH,
                title=f"Observed end state does not contain the claimed effect ({commit})",
                proposition=(
                    f"Claim {claim.id} asserts a side effect for commit {commit}, but observation "
                    f"{not_observed[0].id} records {ProbeOutcome.NOT_OBSERVED.value} for that "
                    "version."
                ),
                rationale=(
                    f"NYX observed the end state at commit {commit} and the claimed effect is not "
                    f"there ({not_observed[0].id}). The observed state does not match the claimed "
                    "effect; this is an inconsistency between the claim and the observation, not "
                    "proof that the action failed."
                ),
                next_step=(
                    f"Investigate why the claimed effect is absent from the observed state at "
                    f"commit {commit} before treating the claim as satisfied."
                ),
                limitations=[
                    "The observation confirms the end state, not what caused it: a different path "
                    "may have produced or removed the effect.",
                    "A not_observed result is an absence in the observed state, not proof the "
                    "action failed.",
                ],
            )

        if mismatched:
            return _finding_result(
                context=context,
                claim=claim,
                commit=commit,
                observations=observations,
                severity=Severity.MEDIUM,
                title=f"Observation does not cover the claimed version ({commit})",
                proposition=(
                    f"Claim {claim.id} asserts a side effect for commit {commit}, but the linked "
                    f"observation ({mismatched[0].id}) targets subject version "
                    f"{mismatched[0].subject_version}."
                ),
                rationale=(
                    f"The only observations linked to claim {claim.id} cover other subject "
                    f"versions, so none of them can confirm the claimed effect for commit "
                    f"{commit}. This is a mismatch between the observation and the claim, not an "
                    "assertion that the effect is absent."
                ),
                next_step=(
                    f"Run a probe against commit {commit} and link the observation to this claim."
                ),
                limitations=[
                    "The observation is real but covers a different version; it says nothing about "
                    f"commit {commit}.",
                    "The claimed effect may be present; this critic cannot see it from the linked "
                    "records.",
                ],
            )

        return _finding_result(
            context=context,
            claim=claim,
            commit=commit,
            observations=observations,
            severity=Severity.MEDIUM,
            title=f"Probe did not complete for the claimed side effect ({commit})",
            proposition=(
                f"Claim {claim.id} asserts a side effect for commit {commit}, and the linked "
                f"observation ({observations[0].id}) did not complete "
                f"({observations[0].result.value})."
            ),
            rationale=(
                f"An observation covers commit {commit}, but its probe did not complete, so it "
                "cannot confirm or refute the claimed effect. The claim is unverified rather than "
                "refuted."
            ),
            next_step=(
                f"Re-run the probe against commit {commit} and link the completed observation to "
                "this claim."
            ),
            limitations=[
                "A probe that errored or was skipped is not a probe that found nothing.",
                "The claimed effect may be present; this critic cannot see it from the linked "
                "records.",
            ],
        )


def _confirmed_result(
    context: CriticContext,
    claim: Claim,
    commit: str,
    observation: Observation,
) -> CriticResult:
    """Build the ``no_finding`` result for a claimed effect an observation confirms."""

    return CriticResult(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        result=CriticResultStatus.NO_FINDING,
        proposition=(
            f"Claim {claim.id} asserts a side effect for commit {commit}, and observation "
            f"{observation.id} confirms it."
        ),
        scope=context.scope,
        observations=[
            f'Claim {claim.id} states: "{claim.statement}"',
            f"Observation {observation.id} was gathered by probe {observation.probe_id} at "
            f"subject version {observation.subject_version} and records "
            f"{observation.result.value}.",
            f"Verification strength achieved: {observation.verification_strength.value}.",
        ],
        rationale=(
            f"Observation {observation.id} covers commit {commit} and records "
            f"{observation.detail or observation.result.value}, so the claimed effect is confirmed "
            "at the observed end state. This means the claim and the observation agree; it is not "
            "a certification that this action caused the effect, and it is not proof that no other "
            "critic could find something."
        ),
        recommended_next_step="No action required from this critic.",
        claim_ids=[claim.id],
        event_ids=[context.target_event.id],
        affected_system_ids=[claim.claimant_system_id],
        observation_refs=[observation.id],
        subject_version=commit,
        verification_strength=observation.verification_strength,
    )
