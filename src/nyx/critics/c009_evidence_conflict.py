"""C-009 — Evidence conflict (SPEC §8).

Detects supporting and counterevidence that materially disagree: a passing and a failing
record for the same defined test under a definitely comparable scope (commit and
environment equal whenever both sides declare them).

When the conflict is determinable the critic does NOT pick a winner and does NOT return a
verdict: it returns ``inconclusive`` and requests verification. A critic that guessed on a
conflict would be worse than one that abstains, so abstention is the designed outcome.
Consistent evidence — all passing, all failing, or a single readable record — stays
quiet, as do records for different tests or for scopes that are not definitely comparable
(different environments do not automatically conflict).

A ``no_finding`` result means only that this critic found no determinable conflict within
its declared scope; it is never a statement that the evidence is consistent everywhere, or
that no other critic could find something.

**Shared root cause.** C-002 compares *claims*; this critic compares *evidence*. The boundary is
deliberate and unchanged. Both key their findings on the same ``root_cause_key`` from
:mod:`nyx.critics.claim_graph`, so one underlying conflict cannot raise two findings.
"""

from __future__ import annotations

import re

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.claim_graph import outcome_fact_key
from nyx.critics.context import CriticContext
from nyx.domain.base import Scope
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    EvidenceKind,
    FindingCategory,
    Severity,
    VerificationStrength,
)
from nyx.domain.evidence import EvidenceItem

CRITIC_ID = "c009-evidence-conflict"
CRITIC_VERSION = "1.0.0"

_VERIFICATION_KINDS = {EvidenceKind.TEST_RESULT, EvidenceKind.CI_TEST_RESULT}

_SUCCESS_MARKER = re.compile(r"\bpass(?:es|ed|ing)?\b|\bgreen\b")
_FAILURE_MARKER = re.compile(r"\bfail(?:s|ed|ing|ure)?\b|\bbroken\b|\bred\b")


def _recorded_outcome(evidence: EvidenceItem) -> str | None:
    """Read the recorded test outcome from the evidence title and description.

    Mirrors C-001: the evidence model has no structured outcome field, so explicit
    word-boundary markers are used. Returns ``"passed"``, ``"failed"``, or ``None``
    when no readable outcome is recorded.
    """

    text = f"{evidence.title} {evidence.description}".lower()
    if _FAILURE_MARKER.search(text):
        return "failed"
    if _SUCCESS_MARKER.search(text):
        return "passed"
    return None


def _test_key(item: EvidenceItem) -> tuple[str, str, EvidenceKind, str]:
    """Group key for "the same defined test": system, artifact, kind, normalised title.

    Unchanged, and deliberately so: C-007 uses the same grouping, and three critics inventing
    three names for one defined test is how a conflict stops being comparable across critics.
    """

    scope = item.scope
    return (
        scope.system_id if scope and scope.system_id else "",
        scope.artifact if scope and scope.artifact else "",
        item.kind,
        " ".join(item.title.lower().split()),
    )


def _comparable(left: EvidenceItem, right: EvidenceItem) -> bool:
    """Definite comparability: commit and environment equal whenever BOTH sides declare them.

    Following the contradictory-records critic's house rule, records for different declared
    environments or commits do not automatically conflict; a missing scope on either side
    makes the pair not determinably comparable.
    """

    l_scope = left.scope
    r_scope = right.scope
    if l_scope is None or r_scope is None:
        return False
    if l_scope.commit and r_scope.commit and l_scope.commit != r_scope.commit:
        return False
    return not (
        l_scope.environment and r_scope.environment and l_scope.environment != r_scope.environment
    )


def _artifact_name(item: EvidenceItem) -> str:
    """The declared artifact of a run, or a placeholder when undeclared."""

    if item.scope and item.scope.artifact:
        return item.scope.artifact
    return "<unknown>"


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no issue within its declared scope. It
    is never a statement that the evidence is consistent everywhere, or that no other
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


class EvidenceConflictCritic:
    """Report materially disagreeing evidence as inconclusive, never as a verdict."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Detect supporting and counterevidence that materially disagree for the same defined "
        "test; return inconclusive and request verification instead of picking a winner."
    )
    scope = CriticScope(
        can_assess=[
            "verification runs (test_result, ci_test_result) linked to the event or claim",
            "passing and failing records for the same defined test under a definitely "
            "comparable scope",
        ],
        cannot_assess=[
            "which side of a determinable conflict is correct",
            "runs whose recorded outcome cannot be read from title or description",
            "records for different tests, artifacts, systems, or not-comparable scopes",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def run(self, context: CriticContext) -> CriticResult:
        runs = [
            item
            for item in context.linked_evidence
            if item.kind in _VERIFICATION_KINDS and _recorded_outcome(item) is not None
        ]
        if len(runs) < 2:
            return _no_finding(
                "Fewer than two linked verification runs record a readable outcome.",
                "This critic compares passing and failing records for the same defined test; "
                "with fewer than two readable linked runs there is nothing to compare within "
                "this critic's declared scope.",
                context.scope,
            )

        by_test: dict[tuple[str, str, EvidenceKind, str], list[EvidenceItem]] = {}
        for item in runs:
            by_test.setdefault(_test_key(item), []).append(item)

        for key in sorted(by_test):
            group = sorted(by_test[key], key=lambda item: (item.captured_at, item.id))
            passes = [item for item in group if _recorded_outcome(item) == "passed"]
            failures = [item for item in group if _recorded_outcome(item) == "failed"]
            if not passes or not failures:
                continue
            comparable_failures = [
                item for item in failures if any(_comparable(item, p) for p in passes)
            ]
            if not comparable_failures:
                continue
            failing = comparable_failures[-1]
            comparable_passes = [p for p in passes if _comparable(failing, p)]
            passing = comparable_passes[-1]
            return self._conflict(passing, failing, context)

        return _no_finding(
            "No linked evidence materially disagrees for the same defined test and scope.",
            "Every readable linked verification run for a defined test records the same "
            "outcome, or the disagreeing records are not definitely comparable. This "
            "critic found nothing within its declared scope.",
            context.scope,
        )

    def _conflict(
        self, passing: EvidenceItem, failing: EvidenceItem, context: CriticContext
    ) -> CriticResult:
        """Build the inconclusive conflict result. The critic abstains by design."""

        title = " ".join(failing.title.lower().split())
        artifact = _artifact_name(failing)
        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.INCONCLUSIVE,
            proposition=(
                f"Evidence for '{title}' ({artifact}) materially disagrees: {passing.id} "
                f"records a passing outcome and {failing.id} records a failing outcome; the "
                "conflict cannot be resolved deterministically."
            ),
            scope=context.scope,
            observations=[
                f"Evidence {passing.id} records a passing outcome for '{title}', captured "
                f"at {passing.captured_at}.",
                f"Evidence {failing.id} records a failing outcome for '{title}', captured "
                f"at {failing.captured_at}.",
                "Both records are for the same defined test under a definitely comparable "
                "declared scope.",
                "This critic does not pick a winner; the stored records disagree.",
            ],
            limitations=[
                "Abstention is deliberate, not a failed analysis: a guessed winner would be "
                "worse than an honest inconclusive.",
                "Outcomes are read from evidence titles/descriptions with explicit markers; "
                "nuanced or structured results may be missed (same limitation as C-001).",
                "Records that do not declare a comparable commit and environment are not "
                "treated as conflicting (different environments do not automatically "
                "conflict).",
            ],
            rationale=(
                f"The linked evidence for '{title}' ({artifact}) contains materially "
                f"disagreeing outcomes: {passing.id} passed and {failing.id} failed under a "
                "comparable declared scope. Which record reflects current reality cannot "
                "be determined from the stored data, so the critic reports the conflict as "
                "inconclusive instead of guessing."
            ),
            recommended_next_step=(
                f"Request an independent verification run for '{title}' ({artifact}) and "
                "link its result as evidence; do not treat either record as settled."
            ),
            requires_human_review=True,
            category=FindingCategory.CONTRADICTION,
            severity=Severity.HIGH,
            title=f"Conflicting evidence for '{title}' ({artifact})",
            summary=(
                f"Evidence {passing.id} and {failing.id} materially disagree about "
                f"'{title}' ({artifact}); the critic returns inconclusive and requests "
                "verification rather than picking a winner."
            ),
            supporting_evidence_ids=[passing.id],
            counterevidence_ids=[failing.id],
            # Same predicate and subject as C-002, so a claim-level contradiction and an
            # evidence-level conflict about one test are one root cause.
            root_cause_key=outcome_fact_key(predicate="outcome", scope=failing.scope).to_key(),
            subject_version=failing.scope.commit if failing.scope else None,
            verification_strength=VerificationStrength.SELF_REPORTED,
            claim_ids=[context.linked_claim.id] if context.linked_claim else [],
            event_ids=[context.target_event.id],
            affected_system_ids=[context.scope.system_id],
        )
