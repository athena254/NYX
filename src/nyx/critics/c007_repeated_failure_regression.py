"""C-007 — Repeated failure / regression (SPEC §8).

Detects a previously passing verification check that now fails for the same defined test.
The regression call is only made when the passing run is genuinely *prior* to the failing
run; when the two runs were captured under different declared commits or environments the
critic still reports the pass-then-fail sequence, but surfaces the difference as an explicit
CONFOUNDER — it never hides it and never presents a confounded pair as a clean regression.

A "defined test" is identified by (system, artifact, evidence kind, normalised title): the
evidence model carries no structured test identifier, so the title is the only name
available. That is a documented limitation, not a hidden assumption.

Three rules, decided in this order:

* RULE 1 (pass then fail): a defined test has a readable passing outcome followed by a later
  readable failing outcome. This is the 1.0.0 behaviour, unchanged.
* RULE 2 (flaky vs regression): the pair is classified. Two runs of the same test at the
  **same** declared commit and environment that disagree are *flaky* — an intermittent
  failure, not a regression. A failure that is new after a change of commit is a *regression*.
  The classification is stated in the finding rather than left to the reader, and recorded as
  ``Reproducibility.FLAKY`` or ``Reproducibility.DETERMINISTIC``.
* RULE 3 (recurrence escalation): failures are clustered by a normalised *failure signature*
  (the run's own title and description, lowercased, whitespace-collapsed, punctuation
  stripped) over a configurable window. When one signature recurs at least
  ``recurrence_threshold`` times inside the window, the severity is escalated, because a
  failure that keeps coming back is a stronger signal than one that appeared once.

The recorded outcome is read from the evidence title/description with explicit word-boundary
markers — the same documented limitation as C-001.

A ``no_finding`` result means only that this critic found no regression within its declared
scope; it is never a statement that the artifact is healthy, or that no other critic could
find something.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Digest, Scope
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    EvidenceKind,
    FindingCategory,
    Reproducibility,
    Severity,
)
from nyx.domain.evidence import EvidenceItem

CRITIC_ID = "c007-repeated-failure-regression"
CRITIC_VERSION = "1.1.0"

_VERIFICATION_KINDS = {EvidenceKind.TEST_RESULT, EvidenceKind.CI_TEST_RESULT}

_SUCCESS_MARKER = re.compile(r"\bpass(?:es|ed|ing)?\b|\bgreen\b")
_FAILURE_MARKER = re.compile(r"\bfail(?:s|ed|ing|ure)?\b|\bbroken\b|\bred\b")

#: Characters dropped when building a failure signature. A signature is a normalised form of
#: what the evidence actually declares, so two runs of the same failure described with
#: different punctuation or spacing still cluster together.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: Classification labels reported in the finding.
FLAKY = "flaky"
REGRESSION = "regression"

#: Base severity for each classification, before recurrence escalation.
_BASE_SEVERITY = {FLAKY: Severity.MEDIUM, REGRESSION: Severity.HIGH}

#: Severity a recurring cluster is escalated to, for either classification.
_ESCALATED_SEVERITY = Severity.CRITICAL


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


def _failure_signature(item: EvidenceItem) -> str:
    """Normalise a failing run into the signature its recurrence is clustered by.

    The signature is derived from what the evidence *declares* — its title and description —
    because the evidence model has no structured error field. Lowercasing, collapsing
    whitespace and dropping punctuation means two runs of the same failure described with
    different wording or spacing still land in one cluster. A run that declares nothing
    readable produces an empty signature and is never clustered, so an unreadable record is
    not silently merged with a readable one.
    """

    text = f"{item.title} {item.description}".lower()
    return _NON_ALNUM.sub(" ", text).strip()


def _signature_key(signature: str) -> Digest:
    """The stable ``root_cause_key`` for one failure signature."""

    return f"sha256:{hashlib.sha256(signature.encode('utf-8')).hexdigest()}"


def _classify(passing: EvidenceItem, failing: EvidenceItem) -> str:
    """Classify a pass-then-fail pair as flaky or a regression.

    Only *definitely declared* scope decides it, and only when both runs declare the field:

    * same commit **and** same environment -> ``flaky``. The artifact did not change and the
      environment did not change, so the disagreement is an intermittent failure rather than a
      regression introduced by a change.
    * different declared commits -> ``regression``. The failure is new after a change.

    A pair that declares neither, or declares only one side of a field, cannot be classified
    on this evidence. It is reported as a regression with the scope difference as a CONFOUNDER
    — the 1.0.0 behaviour — rather than guessed into either bucket.
    """

    p_scope = passing.scope
    f_scope = failing.scope
    if p_scope is None or f_scope is None:
        return REGRESSION
    if p_scope.commit and f_scope.commit and p_scope.commit != f_scope.commit:
        return REGRESSION
    same_commit = bool(p_scope.commit) and p_scope.commit == f_scope.commit
    same_environment = bool(p_scope.environment) and p_scope.environment == f_scope.environment
    if same_commit and same_environment:
        return FLAKY
    return REGRESSION


def _recurrence(failures: list[EvidenceItem], signature: str, *, window: timedelta) -> int:
    """Count the runs sharing ``signature`` inside ``window`` ending at the latest failure.

    The window is measured back from the latest failure, so a cluster answers "how often did
    this failure recur recently" rather than "how many times has it ever happened". Runs
    outside the window are excluded, which is what keeps a long-dormant failure from looking
    like an ongoing one.
    """

    if not signature:
        return 0
    latest = max(item.captured_at for item in failures)
    earliest = latest - window
    return sum(
        1
        for item in failures
        if _failure_signature(item) == signature and earliest <= item.captured_at <= latest
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
    """Detect a defined test that passed and now fails, classifying and clustering it."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Detect a previously passing verification check that now fails for the same defined "
        "test; classify the pair as flaky or a regression, report commit and environment "
        "differences as confounders rather than hiding them, and escalate severity when the "
        "same failure signature recurs."
    )
    scope = CriticScope(
        can_assess=[
            "verification runs (test_result, ci_test_result) linked to the event or claim",
            "defined tests identified by system, artifact, kind, and title",
            "whether a pass-then-fail pair shares a declared commit and environment, which is "
            "what separates flaky from regression",
            "how often a normalised failure signature recurs inside a configurable window",
        ],
        cannot_assess=[
            "runs whose recorded outcome cannot be read from title or description",
            "different tests, artifacts, or systems",
            "the cause of a failure once a confounder is present",
            "the *reason* a failure is intermittent: recurrence is counted, not explained",
            "failures whose declared signature is empty, which are never clustered",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def __init__(
        self,
        *,
        window_days: float = 30.0,
        recurrence_threshold: int = 3,
    ) -> None:
        """Construct the critic.

        ``window_days`` bounds how far back a recurrence is counted, measured from the latest
        failure. ``recurrence_threshold`` is the number of failures sharing one signature
        inside that window at which severity is escalated. Both are constructor arguments so a
        deployment can retune them without a code change; the defaults are conservative, and
        with a threshold above any single-run count the 1.0.0 severity behaviour is preserved.
        """

        self.window_days = window_days
        self.recurrence_threshold = recurrence_threshold

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
            return self._regression(prior_passes[-1], latest_failure, failures, context)

        return _no_finding(
            "No linked verification run passes and then fails for the same defined test.",
            "No defined test in the linked evidence has a readable passing outcome followed "
            "by a later readable failing outcome. This critic found nothing within its "
            "declared scope; it does not assert the artifact is healthy.",
            context.scope,
        )

    def _regression(
        self,
        passing: EvidenceItem,
        failing: EvidenceItem,
        all_failures: list[EvidenceItem],
        context: CriticContext,
    ) -> CriticResult:
        """Build the regression result, classifying it and reporting every confounder."""

        title = " ".join(failing.title.lower().split())
        artifact = _artifact_name(failing)
        confounders = _confounders(passing, failing)
        classification = _classify(passing, failing)
        signature = _failure_signature(failing)
        recurrences = _recurrence(all_failures, signature, window=timedelta(days=self.window_days))
        escalated = recurrences >= self.recurrence_threshold
        severity = _ESCALATED_SEVERITY if escalated else _BASE_SEVERITY[classification]
        reproducibility = (
            Reproducibility.FLAKY if classification == FLAKY else (Reproducibility.DETERMINISTIC)
        )

        observations = [
            f"Evidence {passing.id} records a passing outcome for '{title}' "
            f"({_scope_text(passing)}), captured at {passing.captured_at}.",
            f"Evidence {failing.id} records a failing outcome for '{title}' "
            f"({_scope_text(failing)}), captured at {failing.captured_at}.",
            f"Classification: {classification}.",
        ]
        limitations = [
            "Outcomes are read from evidence titles/descriptions with explicit markers; "
            "nuanced or structured results may be missed (same limitation as C-001).",
            "Flaky vs regression is decided only by definitely-declared scope fields; an "
            "undeclared commit or environment is a provenance gap (the provenance-gap "
            "critic's finding), not a classification input.",
        ]
        if confounders:
            observations.extend(f"CONFOUNDER: {text}" for text in confounders)
        if signature:
            observations.append(
                f"The failure signature {signature!r} occurred {recurrences} time(s) inside "
                f"the last {self.window_days:g} day(s); the escalation threshold is "
                f"{self.recurrence_threshold}."
            )
            limitations.append(
                "The failure signature is a normalised form of the run's own title and "
                "description, so two different failures described in the same words are "
                "counted as one cluster."
            )

        if confounders:
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
        elif classification == FLAKY:
            observations.append(
                "Both runs were captured under the same declared commit and environment, so "
                "the failure is intermittent rather than introduced by a change."
            )
            proposition = (
                f"Defined test '{title}' for artifact {artifact} passed in {passing.id} and "
                f"fails in {failing.id} under the same declared commit and environment: a "
                f"flaky test, not a regression."
            )
            limitations.append(
                "Recurrence is counted, not explained: this critic reports that the failure "
                "repeats, not why."
            )
            rationale = (
                f"The same defined test '{title}' for artifact {artifact} recorded a passing "
                f"outcome ({passing.id}) and later a failing outcome ({failing.id}) under the "
                "same declared commit and environment. The artifact did not change and the "
                "environment did not change, so the disagreement is an intermittent failure "
                "rather than a regression introduced by a change."
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

        if escalated:
            limitations.append(
                f"The failure signature recurred {recurrences} times inside the "
                f"{self.window_days:g}-day window, at or above the escalation threshold of "
                f"{self.recurrence_threshold}, so severity is escalated."
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
            severity=severity,
            title=(
                f"Repeated failure regression: '{title}' for {artifact}"
                + (" (confounded)" if confounders else "")
                + (f" ({classification})" if not confounders else "")
            ),
            summary=proposition,
            root_cause_key=_signature_key(signature) if signature else None,
            reproducibility=reproducibility,
            supporting_evidence_ids=[passing.id, failing.id],
            claim_ids=[context.linked_claim.id] if context.linked_claim else [],
            event_ids=[context.target_event.id],
            affected_system_ids=[context.scope.system_id],
        )
