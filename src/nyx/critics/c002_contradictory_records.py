"""C-002 — Contradictory records (SPEC §8).

Detects materially incompatible statements about the same subject **and** the same scope.
The scope test is strict: artifact, commit/version, and environment must all be definitely
equal, and the claims' time periods must overlap. The critic therefore does **not** fire
across different versions, environments, or time periods — a failure record for an older
commit is not a contradiction of a success record for the current one.

Contradiction is judged from explicit success/failure markers in the claim statements with
word boundaries. Mixed statements (both markers, e.g. "tests pass but the build fails") are
not a clean signal and are skipped rather than guessed at.
"""

from __future__ import annotations

import re

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import CriticResultStatus, CriticType, FindingCategory, Severity

CRITIC_ID = "c002-contradictory-records"
CRITIC_VERSION = "1.0.0"

_SUCCESS_MARKER = re.compile(
    r"\bpass(?:es|ed|ing)?\b|\bdone\b|\bfixed\b|\bdeployed\b|\bgreen\b|\bcomplete(?:d)?\b"
    r"|\bsuccess(?:ful)?\b"
)
_FAILURE_MARKER = re.compile(
    r"\bfail(?:s|ed|ing|ure)?\b|\bbroken\b|\bred\b|\bnot done\b|\bnot passing\b"
    r"|\bincomplete\b|\berror(?:s|ed)?\b|\bunstable\b"
)


def _asserts(statement: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.search(statement.lower()))


def _stance(claim: Claim) -> str | None:
    """Return 'success', 'failure', or None when the statement is mixed or neutral."""

    statement = claim.statement.lower()
    success = _asserts(statement, _SUCCESS_MARKER)
    failure = _asserts(statement, _FAILURE_MARKER)
    if success and not failure:
        return "success"
    if failure and not success:
        return "failure"
    return None


def _same_scope(left: Claim, right: Claim) -> bool:
    """Strict same-scope test: artifact, commit, and environment all definitely equal."""

    if not (left.scope.artifact and right.scope.artifact):
        return False
    if left.scope.artifact != right.scope.artifact:
        return False
    if not (left.scope.commit and right.scope.commit):
        return False
    if left.scope.commit != right.scope.commit:
        return False
    if not (left.scope.environment and right.scope.environment):
        return False
    if left.scope.environment != right.scope.environment:
        return False
    return _periods_overlap(left, right)


def _periods_overlap(left: Claim, right: Claim) -> bool:
    """Return False only when both claims have periods that definitely do not overlap."""

    if left.scope.period is None or right.scope.period is None:
        return True
    left_to = left.scope.period.to
    right_to = right.scope.period.to
    disjoint = (left_to is not None and right.scope.period.from_ > left_to) or (
        right_to is not None and left.scope.period.from_ > right_to
    )
    return not disjoint


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no issue within its declared scope. It
    is never a statement that the claims are consistent everywhere, or that no other critic
    could find something.
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


class ContradictoryRecordsCritic:
    """Detect incompatible statements about the same subject and scope."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Detect materially incompatible statements about the same subject and scope, without "
        "firing across different versions, environments, or time periods."
    )
    scope = CriticScope(
        can_assess=[
            "claims that name the same artifact, commit, and environment as the target claim",
        ],
        cannot_assess=[
            "claims about different commits or versions",
            "claims in different environments",
            "claims whose time periods do not overlap",
            "claims with mixed or neutral statements",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def run(self, context: CriticContext) -> CriticResult:
        claim = context.linked_claim
        if claim is None:
            return _no_finding(
                "No claim is linked to this event.",
                "This critic compares a linked claim against other claims about the same "
                "subject and scope; without a linked claim there is nothing to compare.",
                context.scope,
            )

        target_stance = _stance(claim)
        if target_stance is None:
            return _no_finding(
                f"Claim {claim.id} does not make a clean success or failure statement.",
                "The target claim's statement is mixed or neutral, so this critic cannot "
                "treat it as a clean assertion and finds nothing within its declared scope.",
                context.scope,
            )

        for other in context.related_claims:
            other_stance = _stance(other)
            if other_stance is None or other_stance == target_stance:
                continue
            if not _same_scope(claim, other):
                continue

            success_claim, failure_claim = (
                (claim, other) if target_stance == "success" else (other, claim)
            )
            return CriticResult(
                critic_id=CRITIC_ID,
                critic_version=CRITIC_VERSION,
                result=CriticResultStatus.FINDING,
                proposition=(
                    f"Claims {success_claim.id} and {failure_claim.id} make materially "
                    f"incompatible statements about {claim.scope.artifact} at commit "
                    f"{claim.scope.commit}."
                ),
                scope=claim.scope,
                observations=[
                    f'Claim {success_claim.id} asserts success: "{success_claim.statement}"',
                    f'Claim {failure_claim.id} asserts failure: "{failure_claim.statement}"',
                    f"Both claims scope themselves to artifact {claim.scope.artifact}, "
                    f"commit {claim.scope.commit}, environment {claim.scope.environment}.",
                ],
                inferences=[
                    "The two statements cannot both be true for the same subject and scope.",
                ],
                contradiction=(
                    f"Claim {success_claim.id} asserts success while claim "
                    f"{failure_claim.id} asserts failure for {claim.scope.artifact} at commit "
                    f"{claim.scope.commit} in environment {claim.scope.environment}."
                ),
                limitations=[
                    "Statement stances are read from explicit success/failure markers; "
                    "nuanced wording may be missed.",
                ],
                rationale=(
                    f"Two stored claims about the same artifact, commit, and environment "
                    f"assert opposite outcomes ({success_claim.id} vs {failure_claim.id}). "
                    "At least one statement is stale or incorrect for the shared scope."
                ),
                recommended_next_step=(
                    "Request clarification or a current verification run for commit "
                    f"{claim.scope.commit}, then link the result as evidence."
                ),
                requires_human_review=True,
                category=FindingCategory.CONTRADICTION,
                severity=Severity.HIGH,
                title=(
                    f"Contradictory claims about {claim.scope.artifact} at commit "
                    f"{claim.scope.commit}"
                ),
                summary=(
                    f"Claim {success_claim.id} asserts success and claim {failure_claim.id} "
                    f"asserts failure for {claim.scope.artifact} at commit "
                    f"{claim.scope.commit} ({claim.scope.environment})."
                ),
                claim_ids=[claim.id, other.id],
                event_ids=[context.target_event.id],
                affected_system_ids=[claim.claimant_system_id],
            )

        return _no_finding(
            f"Claim {claim.id} has no contradictory counterpart in the same scope.",
            "No other stored claim about the same artifact, commit, environment, and "
            "overlapping period asserts the opposite outcome. Claims for other versions or "
            "environments are out of this critic's declared scope.",
            context.scope,
        )
