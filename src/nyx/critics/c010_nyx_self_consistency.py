"""C-010 — NYX self-consistency (SPEC §8).

NYX checking itself. Four checks against the configured storage model:

1. **Valid evidence references.** Every stored finding's evidence id references resolve to
   a stored evidence item, and a ``confirmed`` finding carries a rationale — at least one
   recorded critic run with a non-empty rationale (safety invariant 1).
2. **Valid state transitions.** For every finding, the recorded ``finding.transition``
   audit history replays from ``detected`` through the SPEC §6 transition graph, and the
   replayed final state equals the stored status. A non-detected stored status with no
   recorded transitions is a violation: the documented model derives the row's status
   from the latest valid transition.
3. **Critic versions recorded.** Every critic run row carries a non-empty
   ``critic_version`` (SPEC §5.6), and every finding has at least one critic run.
4. **Complete audit chain.** Audit sequences are contiguous from 1, each entry's
   ``prior_entry_hash`` links to the previous entry's ``entry_hash``, and every
   ``entry_hash`` recomputes from the stored fields via the audit writer's own hash
   function (imported, so the check and the writer cannot diverge).

Access: unlike the other critics this one reads the store, so it is constructed with the
application's ``SessionFactory``; ``run`` opens its own transaction via ``session_scope``.
The context supplies the assessment scope only — the checks are store-global because a
self-consistency violation anywhere undermines trust in every confirmed finding.

A ``no_finding`` result means only that this critic found no self-consistency issue within
its declared scope; it is never a statement that the stored content is true, and no other
critic could not find something.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from nyx.audit.writer import _entry_hash
from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.audit import AuditTarget
from nyx.domain.base import PrincipalRef, Scope
from nyx.domain.enums import (
    AuditOutcome,
    CriticResultStatus,
    CriticType,
    FindingCategory,
    FindingStatus,
    Severity,
)
from nyx.findings.lifecycle import allowed_transitions
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories._mapping import as_utc
from nyx.storage.tables import AuditRow, CriticRunRow, EvidenceRow, FindingRow

CRITIC_ID = "c010-nyx-self-consistency"
CRITIC_VERSION = "1.0.0"

_TRANSITION_ACTION = "finding.transition"
_TRANSITION_RE = re.compile(r"lifecycle transition ([a-z_]+) -> ([a-z_]+)")


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no self-consistency issue within its
    declared scope. It is never a statement that the stored content is true, or that no
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


class NyxSelfConsistencyCritic:
    """Check NYX's own stored model for internal consistency violations."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Check that every confirmed finding has valid evidence references and a rationale, "
        "that state transitions are valid, that critic versions are recorded, and that the "
        "audit chain is complete within the configured storage model."
    )
    scope = CriticScope(
        can_assess=[
            "stored findings, critic runs, evidence references, and audit entries in the "
            "configured storage model",
        ],
        cannot_assess=[
            "the truth of stored content",
            "storage outside the configured model",
            "authorisation or capability decisions",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._session_factory = session_factory

    def run(self, context: CriticContext) -> CriticResult:
        if self._session_factory is None:
            return CriticResult(
                critic_id=CRITIC_ID,
                critic_version=CRITIC_VERSION,
                result=CriticResultStatus.ERROR,
                proposition="C-010 could not run: no storage session factory was provided.",
                scope=context.scope,
                observations=["NyxSelfConsistencyCritic was constructed without a SessionFactory."],
                rationale=(
                    "This critic reads NYX's own store; without a session factory it cannot "
                    "perform any check, so it reports an error rather than silently "
                    "passing."
                ),
                recommended_next_step=(
                    "Construct NyxSelfConsistencyCritic with the application's SessionFactory."
                ),
            )
        with session_scope(self._session_factory) as session:
            violations = self._check_store(session)
        if violations:
            return self._violation_result(violations, context)
        return _no_finding(
            "The stored model is self-consistent within the configured storage model.",
            "Every stored finding's evidence references resolve, every confirmed finding "
            "carries a rationale, every recorded transition replays legally from 'detected' "
            "to the stored status, every critic run records a critic version, and the "
            "audit chain is contiguous and hash-complete. This means only that the store "
            "is internally consistent — not that any finding's content is true.",
            context.scope,
        )

    def _check_store(self, session: Session) -> list[str]:
        """Run all four checks and return the violation list (empty means consistent)."""

        findings = list(session.scalars(select(FindingRow).order_by(FindingRow.id)))
        runs = list(session.scalars(select(CriticRunRow).order_by(CriticRunRow.id)))
        audits = list(session.scalars(select(AuditRow).order_by(AuditRow.sequence)))
        evidence_ids = set(session.scalars(select(EvidenceRow.id)))

        violations: list[str] = []
        violations.extend(self._check_audit_chain(audits))
        violations.extend(self._check_findings(findings, runs, evidence_ids))
        violations.extend(self._check_transitions(findings, audits))
        return violations

    @staticmethod
    def _check_audit_chain(audits: list[AuditRow]) -> list[str]:
        """Check sequence contiguity, hash linkage, and hash recomputation."""

        violations: list[str] = []
        previous_hash: str | None = None
        for position, entry in enumerate(audits, start=1):
            if entry.sequence != position:
                violations.append(
                    f"audit entry {entry.sequence} sits at position {position}: the "
                    "sequence is not contiguous"
                )
            if entry.prior_entry_hash != previous_hash:
                violations.append(
                    f"audit entry {entry.sequence} prior_entry_hash does not link to the "
                    "previous entry's entry_hash"
                )
            recomputed = _entry_hash(
                schema_version=entry.schema_version,
                sequence=entry.sequence,
                actor=PrincipalRef.model_validate(entry.actor),
                action=entry.action,
                target=AuditTarget(type=entry.target_type, id=entry.target_id),
                timestamp=as_utc(entry.timestamp),
                request_id=entry.request_id,
                correlation_id=entry.correlation_id,
                outcome=AuditOutcome(entry.outcome),
                reason=entry.reason,
                prior_entry_hash=entry.prior_entry_hash,
            )
            if recomputed != entry.entry_hash:
                violations.append(
                    f"audit entry {entry.sequence} entry_hash does not recompute from its "
                    "stored fields: the entry was modified after it was written"
                )
            previous_hash = entry.entry_hash
        return violations

    @staticmethod
    def _check_findings(
        findings: list[FindingRow], runs: list[CriticRunRow], evidence_ids: set[str]
    ) -> list[str]:
        """Check evidence references, confirmed-finding rationales, and critic versions."""

        violations: list[str] = []
        runs_by_finding: dict[str, list[CriticRunRow]] = {}
        for run in runs:
            if not run.critic_version.strip():
                violations.append(f"critic run {run.id} records no critic version")
            runs_by_finding.setdefault(run.finding_id or "", []).append(run)
        for row in findings:
            referenced = (
                set(row.evidence_ids)
                | set(row.supporting_evidence_ids)
                | set(row.counterevidence_ids)
            )
            unknown = sorted(ref for ref in referenced if ref not in evidence_ids)
            if unknown:
                violations.append(
                    f"finding {row.id} references evidence ids that do not exist: "
                    f"{', '.join(unknown)}"
                )
            if row.status == FindingStatus.CONFIRMED.value:
                linked = runs_by_finding.get(row.id, [])
                if not linked:
                    violations.append(
                        f"confirmed finding {row.id} has no recorded critic run and "
                        "therefore no recorded rationale"
                    )
                elif any(not run.rationale.strip() for run in linked):
                    violations.append(
                        f"confirmed finding {row.id} has a critic run with an empty rationale"
                    )
            if row.id not in runs_by_finding:
                violations.append(f"finding {row.id} has no recorded critic run")
        return violations

    @staticmethod
    def _check_transitions(findings: list[FindingRow], audits: list[AuditRow]) -> list[str]:
        """Replay each finding's recorded transition history through the SPEC §6 graph."""

        violations: list[str] = []
        status_by_id: dict[str, str] = {}
        for row in findings:
            try:
                FindingStatus(row.status)
            except ValueError:
                violations.append(f"finding {row.id} has an unknown status '{row.status}'")
                continue
            status_by_id[row.id] = row.status

        transitions: dict[str, list[AuditRow]] = {}
        for entry in audits:
            if entry.action == _TRANSITION_ACTION and entry.target_type == "finding":
                transitions.setdefault(entry.target_id, []).append(entry)

        for finding_id, entries in transitions.items():
            if finding_id not in status_by_id:
                violations.append(
                    f"audit history records transitions for unknown finding {finding_id}"
                )
                continue
            entries.sort(key=lambda entry: entry.sequence)
            state = FindingStatus.DETECTED
            broken = False
            for entry in entries:
                match = _TRANSITION_RE.search(entry.reason)
                if match is None:
                    violations.append(
                        f"audit entry {entry.sequence} for finding {finding_id} does not "
                        "record a parseable lifecycle transition"
                    )
                    broken = True
                    break
                source = FindingStatus(match.group(1))
                target = FindingStatus(match.group(2))
                if source != state:
                    violations.append(
                        f"audit entry {entry.sequence} for finding {finding_id} records "
                        f"a transition out of '{source.value}' but the replayed state was "
                        f"'{state.value}'"
                    )
                    broken = True
                    break
                if target not in allowed_transitions(state):
                    violations.append(
                        f"audit entry {entry.sequence} for finding {finding_id} records "
                        f"illegal transition '{source.value} -> {target.value}' per the "
                        "SPEC §6 graph"
                    )
                    broken = True
                    break
                state = target
            if not broken and status_by_id[finding_id] != state.value:
                violations.append(
                    f"finding {finding_id} is stored as '{status_by_id[finding_id]}' but "
                    f"its recorded transitions replay to '{state.value}'"
                )

        for row in findings:
            if row.id in transitions or row.status == FindingStatus.DETECTED.value:
                continue
            violations.append(
                f"finding {row.id} is stored as '{row.status}' but no lifecycle "
                "transition is recorded for it"
            )
        return violations

    @staticmethod
    def _violation_result(violations: list[str], context: CriticContext) -> CriticResult:
        """Build the finding that reports the violations (bounded to the result model)."""

        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"NYX self-consistency check found {len(violations)} violation(s) in the "
                "stored model."
            ),
            scope=context.scope,
            observations=violations[:64],
            limitations=[
                "This is a check of the stored model's internal consistency, not of the "
                "truth of any finding's content.",
            ],
            rationale=(
                "The stored model violates at least one documented invariant (evidence "
                "references, transition validity, recorded critic versions, or audit-chain "
                "completeness). A store that fails self-consistency cannot certify its own "
                "confirmed findings, so the violations must be repaired."
            ),
            recommended_next_step=(
                "Repair the stored records or replay the missing transitions; do not rely "
                "on confirmed findings until the store passes this check."
            ),
            requires_human_review=True,
            category=FindingCategory.OTHER,
            severity=Severity.HIGH,
            title="NYX self-consistency violation",
            summary=(
                f"{len(violations)} self-consistency violation(s) in the stored model; "
                "confirmed findings cannot be trusted until the store is consistent."
            ),
            affected_system_ids=[context.scope.system_id],
        )
