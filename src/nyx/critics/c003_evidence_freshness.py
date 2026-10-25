"""C-003 — Evidence freshness (SPEC §8).

Flags evidence whose declared scope or validity window does not cover the claim being
evaluated. A test report captured for an older commit, captured outside the claim's validity
window, or scoped to files the claim's change never touched cannot silently verify a newer
artifact — the critic says so explicitly instead of letting the evidence pass as coverage.

Three rules, in the order they are decided:

* RULE 1 (validity window): the evidence's declared window provably starts after the claim's
  evaluation time, or ends before the claim's evaluation time or before the claim's period
  ends.
* RULE 2 (subject version): both sides declare a commit and the two commits differ. The
  version difference *is* the freshness failure; the capture time is corroborating evidence
  carried in the observation, not the trigger. Before the ground-truth layer this rule fired
  only when the evidence was also captured before the claim was made, which was a timestamp
  proxy for staleness: evidence captured after the claim was made was silently accepted.
* RULE 3 (covered scope): both sides declare ``Scope.covered`` and the claim's change touched
  a path the evidence's covered set does not include. Gated by ``require_coverage_scope``.

Deliberately conservative. A version difference is a freshness observation, not evidence of
misconduct; RULE 3 fires only when **both** sides declare a covered set, because an undeclared
set is not an empty set and absent telemetry is unknown, never a defect. A declared entry covers
its own subpaths (``/srv/app/src`` covers ``/srv/app/src/a.py``), comparison is case-sensitive,
and trailing slashes are normalised because they carry no path meaning. Evidence that declares
no scope is not accused here; missing provenance is C-004's finding.

``CRITIC_VERSION`` was bumped from ``1.0.0`` to ``1.1.0`` to record RULE 3: the covered-scope
rule is new behaviour, so a finding raised by it must not be read as one the 1.0.0 rule set
could have raised. RULE 2 changed in the same release but in the same direction as 1.0.0 — it
can only add findings, never remove one — so it is not named as a separate version.

Limitation: this critic compares declared scope strings, not the artifact itself. A declared
``covered`` entry may be recorded as a directory or a module prefix, so the comparison is a
prefix test and cannot tell a real file from a same-prefix sibling. An evidence item that
covers the change in fact but under a different spelling is not detected here, and a change
whose touched paths the claim does not declare is not assessed at all.

A ``no_finding`` result means **only** that this critic found no issue within its declared
scope. It is never a statement that the evidence proves the claim, that the code is correct,
or that no other critic could find something.
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
CRITIC_VERSION = "1.1.0"


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
    """Check that linked evidence covers the claim's scope and evaluation time.

    ``require_coverage_scope`` switches on RULE 3. It is a constructor argument rather than a
    module constant so a deployment can adopt the covered-scope rule without a code change, and
    so the off-state is testable without monkeypatching. With the flag off this critic behaves
    exactly as it did before ``Scope.covered`` existed.
    """

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Flag evidence whose declared scope or validity window does not cover the claim "
        "being evaluated; when coverage checking is enabled, also flag evidence whose declared "
        "covered scope (files, modules, endpoints) does not include what the claim's change "
        "touched."
    )
    scope = CriticScope(
        can_assess=[
            "validity windows of evidence linked to a claim",
            "declared scope fields of evidence linked to a claim",
            "declared covered scope of evidence and claim, and whether the claim's change "
            "touched a covered path",
        ],
        cannot_assess=[
            "evidence that declares no scope (handled by the provenance-gap critic)",
            "the truth of the evidence content",
            "a change whose touched paths the claim does not declare",
            "changes the evidence covers in fact but under a different declared spelling",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def __init__(self, *, require_coverage_scope: bool = False) -> None:
        self.require_coverage_scope = require_coverage_scope

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
            evidence_commit = evidence.scope.commit if evidence.scope else "<unknown>"
            if evidence.captured_at < claim.created_at:
                captured = "before"
            elif evidence.captured_at > claim.created_at:
                captured = "after"
            else:
                captured = "at the same time as"
            return self._finding(
                claim,
                proposition=(
                    f"Evidence {evidence.id} was captured for a different commit than claim "
                    f"{claim.id} and cannot verify it."
                ),
                observations=[
                    f"Claim {claim.id} names commit {claim.scope.commit} and was made at "
                    f"{claim.created_at}.",
                    f"Evidence {evidence.id} declares commit {evidence_commit} and was "
                    f"captured at {evidence.captured_at}, {captured} the claim was made.",
                ],
                rationale=(
                    f"Evidence {evidence.id} targets commit {evidence_commit}, not the "
                    f"claimed commit {claim.scope.commit}, so it does not cover the claim's "
                    "subject version. The version difference is the freshness failure; the "
                    f"capture time ({evidence.captured_at}) is carried as corroborating "
                    "evidence, not as the trigger. Stale evidence cannot silently verify a "
                    "newer artifact."
                ),
                title=f"Evidence targets a different commit than claim {claim.id}",
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

        uncovered = self._uncovered_paths(context.linked_evidence, claim)
        if uncovered is not None:
            evidence, missing = uncovered
            return self._finding(
                claim,
                proposition=(
                    f"Evidence {evidence.id} declares a covered scope that does not include "
                    f"what claim {claim.id} changed."
                ),
                observations=[
                    f"Claim {claim.id} declares covered={claim.scope.covered}.",
                    f"Evidence {evidence.id} declares covered="
                    f"{evidence.scope.covered if evidence.scope else []}.",
                    f"Paths the claim's change touched that the evidence does not cover: "
                    f"{missing}.",
                ],
                rationale=(
                    f"Both the claim and evidence {evidence.id} declare a covered scope, and "
                    f"the claim's change touched {missing}, which the evidence's covered set "
                    "does not include. The evidence therefore does not cover what changed, "
                    "so it cannot verify the claim as scoped."
                ),
                title=f"Evidence covered scope does not include what claim {claim.id} changed",
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

    def _covers(
        self,
        item: EvidenceItem,
        claim: Claim,
        claim_time: datetime,
        claim_end: datetime | None,
    ) -> bool:
        """Return whether the evidence covers the claim's scope, window, and touched paths.

        RULE 3 is part of coverage, not a separate pass: evidence that covers the claim's commit
        and validity window but not the paths the change touched is not coverage, and the
        covered-scope rule below is what names that gap. When the flag is off, or either side
        declares no covered set, this is exactly the pre-ground-truth check.
        """

        if not EvidenceFreshnessCritic._scope_fields_match(item, claim):
            return False
        if item.valid_from > claim_time:
            return False
        if item.valid_until is not None and item.valid_until < claim_time:
            return False
        if claim_end is not None and item.valid_until is not None and item.valid_until < claim_end:
            return False
        return self._paths_cover(item, claim)

    def _paths_cover(self, item: EvidenceItem, claim: Claim) -> bool:
        """Return whether the evidence's covered set includes every path the claim touched."""

        if not self.require_coverage_scope:
            return True
        if item.scope is None or not item.scope.covered or not claim.scope.covered:
            # An undeclared set is unknown, not empty: only two declared sets are comparable.
            return True
        claimed = _normalise_paths(claim.scope.covered)
        declared = _normalise_paths(item.scope.covered)
        return all(any(_covers_path(entry, path) for entry in declared) for path in claimed)

    @staticmethod
    def _stale_older_commit(items: list[EvidenceItem], claim: Claim) -> EvidenceItem | None:
        """RULE 2: the version difference itself is the freshness failure.

        Both sides must declare a commit; an undeclared commit is unknown, not a mismatch. When
        they differ the evidence does not cover the claim's subject version regardless of when it
        was captured. The capture time is reported in the finding, not used to decide it.
        """

        if not claim.scope.commit:
            return None
        for item in items:
            if item.scope is None or not item.scope.commit:
                continue
            if item.scope.commit != claim.scope.commit:
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

    def _uncovered_paths(
        self, items: list[EvidenceItem], claim: Claim
    ) -> tuple[EvidenceItem, list[str]] | None:
        """RULE 3: the claim's change touched a path the evidence's covered set excludes.

        Fires only when both sides declare a covered set. A declared entry covers its own
        subpaths, so a declared ``/srv/app/src`` admits ``/srv/app/src/a.py``; comparison is
        case-sensitive and trailing slashes are normalised because they carry no path meaning.
        """

        if not self.require_coverage_scope:
            return None
        if not claim.scope.covered:
            return None
        claimed = _normalise_paths(claim.scope.covered)
        # Every linked item must cover the change, so a partial-evidence bundle is a gap rather
        # than a pass: one item that covers it does not make the others cover it too.
        for item in items:
            if item.scope is None or not item.scope.covered:
                continue
            declared = _normalise_paths(item.scope.covered)
            missing = [
                path for path in claimed if not any(_covers_path(entry, path) for entry in declared)
            ]
            if missing:
                return item, missing
        return None


def _normalise_paths(paths: list[str]) -> list[str]:
    """Strip trailing slashes so ``a/b/`` and ``a/b`` are the same path.

    Only the *trailing* separator is removed, and only when something precedes it: a declared
    ``/srv/app/`` and a declared ``/srv/app`` denote the same directory, but a bare ``/`` is the
    filesystem root and stripping it would leave an empty string that no path comparison could
    interpret.
    """

    return [path[:-1] if path != "/" and path.endswith("/") else path for path in paths]


def _covers_path(declared: str, path: str) -> bool:
    """Return whether a declared entry covers ``path`` as itself or as a subpath.

    Case-sensitive, and the boundary must be a path separator: ``/srv/app2`` does not cover
    ``/srv/app``.
    """

    if path == declared:
        return True
    if not declared:
        return False
    return path.startswith(declared) and path[len(declared)] == "/"
