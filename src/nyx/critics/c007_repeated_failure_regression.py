"""C-007 — Repeated failure / regression (SPEC §8).

Detects a previously passing verification check that now fails for the same defined test.
The regression call is only made when the passing run is genuinely *prior* to the failing
run; when the two runs were captured under different declared commits or environments the
critic still reports the pass-then-fail sequence, but surfaces the difference as an explicit
CONFOUNDER — it never hides it and never presents a confounded pair as a clean regression.

A "defined test" is identified by (system, artifact, evidence kind, normalised title): the
evidence model carries no structured test identifier, so the title is the only name
available. That is a documented limitation, not a hidden assumption.

The recorded outcome is read from the evidence title/description with explicit word-boundary
markers — the same documented limitation as C-001.

A ``no_finding`` result means only that this critic found no regression within its declared
scope; it is never a statement that the artifact is healthy, or that no other critic could
find something.
"""

from __future__ import annotations

import re

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Scope
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    EvidenceKind,
    FindingCategory,
    Severity,
)
from nyx.domain.evidence import EvidenceItem

CRITIC_ID = "c007-repeated-failure-regression"
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
    """Group key for "the same defined test": system, artifact, kind, normalised title."""

    scope = item.scope
    return (
        scope.system_id if scope and scope.system_id else "",
        scope.artifact if scope and scope.artifact else "",
        item.kind,
        " ".join(item.title.lower().split()),
    )


def _confounders(passing: EvidenceItem, failing: EvidenceItem) -> list[str]:
    """Return one description per DEFINITE scope difference between the two runs.

    Only differences declared on both sides count as confounders. When one run does not
    declare a field the runs are not comparable on that dimension, but an undeclared field
    is a provenance gap (C-004's finding), not a regression confounder.
    """

    found: list[str] = []
    p_scope = passing.scope
    f_scope = failing.scope
    if p_scope is None or f_scope is None:
        return found
    if p_scope.environment and f_scope.environment and p_scope.environment != f_scope.environment:
        found.append(
            f"the passing run was captured in environment '{p_scope.environment}' but the "
            f"failing run was captured in '{f_scope.environment}'"
        )
    if p_scope.commit and f_scope.commit and p_scope.commit != f_scope.commit:
        found.append(
            f"the passing run was captured at commit '{p_scope.commit}' but the failing run "
            f"was captured at commit '{f_scope.commit}'"
        )
    return found


def _artifact_name(item: EvidenceItem) -> str:
    """The declared artifact of a run, or a placeholder when undeclared."""

    if item.scope and item.scope.artifact:
        return item.scope.artifact
    return "<unknown>"


def _scope_text(item: EvidenceItem) -> str:
    """Render the declared commit/environment of a run, marking undeclared fields."""

    scope = item.scope
    if scope is None:
        return "commit <undeclared>, environment <undeclared>"
    commit = scope.commit if scope.commit else "<undeclared>"
    environment = scope.environment if scope.environment else "<undeclared>"
    return f"commit {commit}, environment {environment}"


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no issue within its declared scope. It
    is never a statement that no regression exists anywhere, or that no other critic could
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


class RepeatedFailureRegressionCritic:
    """Detect a defined test that passed and now fails, surfacing confounders."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Detect a previously passing verification check that now fails for the same defined "
        "test, reporting commit and environment differences as confounders rather than "
        "hiding them."
    )
    scope = CriticScope(
        can_assess=[
            "verification runs (test_result, ci_test_result) linked to the event or claim",
            "defined tests identified by system, artifact, kind, and title",
        ],
        cannot_assess=[
            "runs whose recorded outcome cannot be read from title or description",
            "different tests, artifacts, or systems",
            "the cause of a failure once a confounder is present",
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
                "This critic compares a passing run against a later failing run of the same "
                "defined test; with fewer than two readable linked runs there is nothing to "
                "compare within this critic's declared scope.",
                context.scope,
            )

        by_test: dict[tuple[str, str, EvidenceKind, str], list[EvidenceItem]] = {}
        for item in runs:
            by_test.setdefault(_test_key(item), []).append(item)

        for key in sorted(by_test):
            group = sorted(by_test[key], key=lambda item: (item.captured_at, item.id))
            failures = [item for item in group if _recorded_outcome(item) == "failed"]
            if not failures:
                continue
            latest_failure = failures[-1]
            prior_passes = [
                item
                for item in group
                if _recorded_outcome(item) == "passed"
                and item.captured_at < latest_failure.captured_at
            ]
            if not prior_passes:
                continue
            return self._regression(prior_passes[-1], latest_failure, context)

        return _no_finding(
            "No linked verification run passes and then fails for the same defined test.",
            "No defined test in the linked evidence has a readable passing outcome followed "
            "by a later readable failing outcome. This critic found nothing within its "
            "declared scope; it does not assert the artifact is healthy.",
            context.scope,
        )

    def _regression(
        self, passing: EvidenceItem, failing: EvidenceItem, context: CriticContext
    ) -> CriticResult:
        """Build the regression result, reporting every confounder it can prove."""

        title = " ".join(failing.title.lower().split())
        artifact = _artifact_name(failing)
        confounders = _confounders(passing, failing)
        observations = [
            f"Evidence {passing.id} records a passing outcome for '{title}' "
            f"({_scope_text(passing)}), captured at {passing.captured_at}.",
            f"Evidence {failing.id} records a failing outcome for '{title}' "
            f"({_scope_text(failing)}), captured at {failing.captured_at}.",
        ]
        limitations = [
            "Outcomes are read from evidence titles/descriptions with explicit markers; "
            "nuanced or structured results may be missed (same limitation as C-001).",
        ]
        if confounders:
            observations.extend(f"CONFOUNDER: {text}" for text in confounders)
            proposition = (
                f"Defined test '{title}' for artifact {artifact} passed in {passing.id} and "
                f"later fails in {failing.id}, but the runs are not comparable: "
                + "; ".join(confounders)
                + "."
            )
            limitations.append(
                "The pass and fail runs differ in declared scope, so this is a confounded "
                "regression signal: the scope difference may fully explain the failure rather "
                "than a code regression. The confounder is reported, not hidden."
            )
            rationale = (
                f"The same defined test '{title}' has a passing record ({passing.id}) "
                f"followed by a failing record ({failing.id}), but the records were captured "
                "under different declared scope. The regression is real only if the scope "
                "difference is ruled out; until then the confounder stands."
            )
        else:
            observations.append(
                "Both runs were captured under the same declared commit and environment."
            )
            proposition = (
                f"Defined test '{title}' for artifact {artifact} passed in {passing.id} and "
                f"now fails in {failing.id}: a repeated failure regression."
            )
            limitations.append(
                "Only definitely-declared scope fields are compared; an undeclared commit or "
                "environment is a provenance gap (the provenance-gap critic's finding), not "
                "a confounder."
            )
            rationale = (
                f"The same defined test '{title}' for artifact {artifact} recorded a passing "
                f"outcome ({passing.id}) and later a failing outcome ({failing.id}) under the "
                "same declared commit and environment. The most likely explanation is a "
                "regression in the defined test."
            )
        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=proposition,
            scope=context.scope,
            observations=observations,
            limitations=limitations,
            rationale=rationale,
            recommended_next_step=(
                "Re-run the same defined test under one comparable commit and environment; "
                "if the confounded runs cannot be compared, treat the regression as unproven."
            ),
            requires_human_review=True,
            category=FindingCategory.QUALITY_RISK,
            severity=Severity.HIGH,
            title=(
                f"Repeated failure regression: '{title}' for {artifact}"
                + (" (confounded)" if confounders else "")
            ),
            summary=proposition,
            supporting_evidence_ids=[passing.id, failing.id],
            claim_ids=[context.linked_claim.id] if context.linked_claim else [],
            event_ids=[context.target_event.id],
            affected_system_ids=[context.scope.system_id],
        )
