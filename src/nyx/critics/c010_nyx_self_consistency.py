"""C-010 — NYX self-consistency (SPEC §8).

NYX checking itself. Nine checks against the configured storage model:

1. **Valid evidence references.** Every stored finding's evidence id references resolve to
   a stored evidence item, and a ``confirmed`` finding carries a rationale — at least one
   recorded critic run with a non-empty rationale (safety invariant 1).
2. **Valid observation references.** Every finding's ``observation_ids`` resolve to a stored
   observation. A finding that cites a probe result which does not exist is exactly as
   unverifiable as one citing evidence that does not exist, and the two were checked
   separately only because the columns were added at different times.
3. **Valid state transitions.** For every finding, the recorded ``finding.transition``
   audit history replays from ``detected`` through the SPEC §6 transition graph, and the
   replayed final state equals the stored status. A non-detected stored status with no
   recorded transitions is a violation: the documented model derives the row's status
   from the latest valid transition.
4. **Critic versions recorded.** Every critic run row carries a non-empty
   ``critic_version`` (SPEC §5.6), and every finding has at least one critic run.
5. **Complete audit chain.** Audit sequences are contiguous from 1, each entry's
   ``prior_entry_hash`` links to the previous entry's ``entry_hash``, and every
   ``entry_hash`` recomputes from the stored fields via the audit writer's own hash
   function (imported, so the check and the writer cannot diverge).
6. **No duplicate findings.** Findings that share a non-null ``root_cause_key`` are the same
   underlying defect by construction. When more than one of them is still *active* — and
   neither is recorded as the other's ``duplicate_of`` — the deduplication did not do its
   job, and two rows are asserting one defect.
7. **No contradictory severities for one root cause.** Findings sharing a ``root_cause_key``
   must agree on severity. Two severities for one defect means one of them is wrong, and a
   reader cannot tell which.
8. **No critic version drift.** One ``critic_id`` must not run under two different
   ``critic_version`` values. A drift means the recorded result cannot be attributed to a
   known rule version, which is what SPEC §5.6 exists to make possible.
9. **No judge drift.** A model-backed critic must not run under two different
   ``(model_provider, model_version, prompt_version)`` tuples. The same is true of a
   deterministic critic's ``rule_version``. A drifted judge is the failure the golden set
   exists to catch, so the drift is reported here where it can be seen before a finding
   cites it.

Checks 6–9 are the "meta-findings" the ground-truth upgrade asks for. They are deliberately
narrow: each fires only on a *stored* disagreement between rows NYX itself wrote, never on a
judgement about what a finding says.

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

from nyx.audit.chain import entry_hash
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
from nyx.storage.tables import (
    AuditRow,
    CriticRunRow,
    EvidenceRow,
    FindingRow,
    ObservationRow,
)

CRITIC_ID = "c010-nyx-self-consistency"
CRITIC_VERSION = "1.1.0"

_TRANSITION_ACTION = "finding.transition"
_TRANSITION_RE = re.compile(r"lifecycle transition ([a-z_]+) -> ([a-z_]+)")

#: Statuses in which a finding still asserts a defect. A finding in any other status has been
#: withdrawn, rejected, accepted as risk, closed, expired, or recorded as someone else's
#: duplicate, so it is no longer an active second accusation of the same defect.
_ACTIVE_STATUSES = frozenset(
    {
        FindingStatus.DETECTED.value,
        FindingStatus.EVIDENCE_PENDING.value,
        FindingStatus.CHALLENGED.value,
        FindingStatus.REBUTTAL_PENDING.value,
        FindingStatus.VERIFICATION_PENDING.value,
        FindingStatus.CONFIRMED.value,
        FindingStatus.PARTIALLY_CONFIRMED.value,
        FindingStatus.REMEDIATION_PENDING.value,
        FindingStatus.FIXED_PENDING_VERIFICATION.value,
    }
)


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
        "Check that every confirmed finding has valid evidence and observation references and a "
        "rationale, that state transitions are valid, that critic versions are recorded, that the "
        "audit chain is complete, and that no root cause is asserted twice, with contradictory "
        "severities, or under a drifted critic or judge version."
    )
    scope = CriticScope(
        can_assess=[
            "stored findings, critic runs, evidence references, observation references, and "
            "audit entries in the configured storage model",
            "disagreements between rows NYX itself wrote: duplicate root causes, contradictory "
            "severities, and critic/judge version drift across runs",
        ],
        cannot_assess=[
            "the truth of stored content",
            "storage outside the configured model",
            "authorisation or capability decisions",
            "whether a critic's *rule* is correct, as opposed to consistently versioned",
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
            "Every stored finding's evidence and observation references resolve, every "
            "confirmed finding carries a rationale, every recorded transition replays legally "
            "from 'detected' to the stored status, every critic run records a critic version, "
            "the audit chain is contiguous and hash-complete, and no root cause is asserted by "
            "two active findings, carries two severities, or ran under a drifted critic or "
            "judge version. This means only that the store is internally consistent — not that "
            "any finding's content is true.",
            context.scope,
        )

    def _check_store(self, session: Session) -> list[str]:
        """Run all nine checks and return the violation list (empty means consistent)."""

        findings = list(session.scalars(select(FindingRow).order_by(FindingRow.id)))
        runs = list(session.scalars(select(CriticRunRow).order_by(CriticRunRow.id)))
        audits = list(session.scalars(select(AuditRow).order_by(AuditRow.sequence)))
        evidence_ids = set(session.scalars(select(EvidenceRow.id)))
        observation_ids = set(session.scalars(select(ObservationRow.id)))

        violations: list[str] = []
        violations.extend(self._check_audit_chain(audits))
        violations.extend(self._check_findings(findings, runs, evidence_ids))
        violations.extend(self._check_observation_refs(findings, observation_ids))
        violations.extend(self._check_transitions(findings, audits))
        violations.extend(self._check_duplicate_findings(findings))
        violations.extend(self._check_contradictory_severities(findings))
        violations.extend(self._check_critic_version_drift(runs))
        violations.extend(self._check_judge_drift(runs))
        return violations

    @staticmethod
    def _check_observation_refs(findings: list[FindingRow], observation_ids: set[str]) -> list[str]:
        """Check that every finding's observation references resolve to a stored observation.

        Same rule as the evidence-reference check, applied to the probe results NYX gathered
        itself. A finding that cites an observation which does not exist is not evidence-linked,
        whatever its ``observation_refs`` claims.
        """

        violations: list[str] = []
        for row in findings:
            unknown = sorted(ref for ref in row.observation_ids if ref not in observation_ids)
            if unknown:
                violations.append(
                    f"finding {row.id} references observation ids that do not exist: "
                    f"{', '.join(unknown)}"
                )
        return violations

    @staticmethod
    def _check_duplicate_findings(findings: list[FindingRow]) -> list[str]:
        """Check that no root cause is asserted by more than one active finding.

        ``root_cause_key`` is the shared defect identity: two findings that share one *are* the
        same defect. The store's own deduplication marks the loser with ``status='duplicate'``
        and ``duplicate_of`` pointing at the keeper, so a second *active* row for one key means
        the merge did not happen.

        Only active statuses count. A finding that was rejected, withdrawn, accepted as risk,
        closed, expired, or already marked as someone else's duplicate is not a second
        accusation, and counting it would report a resolved defect as a live one.
        """

        by_key: dict[str, list[FindingRow]] = {}
        for row in findings:
            if row.root_cause_key:
                by_key.setdefault(row.root_cause_key, []).append(row)

        violations: list[str] = []
        for key, rows in sorted(by_key.items()):
            if len(rows) < 2:
                continue
            active = [row for row in rows if row.status in _ACTIVE_STATUSES]
            if len(active) < 2:
                continue
            # The store already knows which row is the keeper when every other active row
            # records a duplicate_of link pointing at an active row. At most one keeper may
            # be unlinked; two unlinked rows are one defect recorded twice.
            active_ids = {row.id for row in active}
            linked = [row for row in active if row.duplicate_of in active_ids]
            if len(active) - len(linked) <= 1:
                continue
            ids = ", ".join(sorted(row.id for row in active))
            violations.append(
                f"root cause {key} is asserted by {len(active)} active findings ({ids}) "
                "without a duplicate_of link between them: one defect is recorded twice"
            )
        return violations

    @staticmethod
    def _check_contradictory_severities(findings: list[FindingRow]) -> list[str]:
        """Check that findings sharing a root cause agree on severity.

        Two severities for one root cause means one of them is wrong, and nothing in the store
        says which. A reader ranking work by severity cannot tell whether the defect is MEDIUM
        or HIGH, which is the whole point of recording the severity at all.
        """

        by_key: dict[str, list[FindingRow]] = {}
        for row in findings:
            if row.root_cause_key:
                by_key.setdefault(row.root_cause_key, []).append(row)

        violations: list[str] = []
        for key, rows in sorted(by_key.items()):
            severities = sorted({row.severity for row in rows})
            if len(severities) > 1:
                detail = ", ".join(
                    f"{row.id}={row.severity}" for row in sorted(rows, key=lambda r: r.id)
                )
                violations.append(
                    f"root cause {key} carries contradictory severities ({', '.join(severities)}): "
                    f"{detail}"
                )
        return violations

    @staticmethod
    def _check_critic_version_drift(runs: list[CriticRunRow]) -> list[str]:
        """Check that one critic id does not run under two different rule versions.

        SPEC §5.6 requires every run to record the critic version so a result can be attributed
        to the rule that produced it. Two versions for one id means that attribution is broken:
        a run recorded last week and a run recorded today were made by different rules, and a
        reader comparing them is comparing two different critics.
        """

        by_critic: dict[str, list[CriticRunRow]] = {}
        for run in runs:
            by_critic.setdefault(run.critic_id, []).append(run)

        violations: list[str] = []
        for critic_id, rows in sorted(by_critic.items()):
            versions = sorted(
                {run.critic_version.strip() for run in rows if run.critic_version.strip()}
            )
            if len(versions) > 1:
                violations.append(
                    f"critic {critic_id} ran under {len(versions)} different critic versions "
                    f"({', '.join(versions)}): recorded results cannot be attributed to one rule"
                )
        return violations

    @staticmethod
    def _check_judge_drift(runs: list[CriticRunRow]) -> list[str]:
        """Check that one judge does not run under two different configurations.

        For a model-backed critic the tuple is ``(model_provider, model_version,
        prompt_version)``; for a deterministic critic it is ``rule_version``. A judge that
        drifts changes what it means for its output to be "the same critic", so a finding
        raised under one configuration cannot be compared with a finding raised under another.

        A field that is empty on every run of a critic is not drift — it is simply not recorded
        — so the comparison is over the configurations that were actually used, and a critic
        that records no provenance at all is reported by check 4 rather than here.
        """

        def _config(run: CriticRunRow) -> tuple[str, ...] | None:
            parts: tuple[str | None, ...]
            if run.critic_type == CriticType.LLM.value:
                parts = (run.model_provider, run.model_version, run.prompt_version)
            else:
                parts = (run.rule_version,)
            present = tuple(part.strip() for part in parts if part and part.strip())
            return present or None

        by_critic: dict[str, list[CriticRunRow]] = {}
        for run in runs:
            by_critic.setdefault(run.critic_id, []).append(run)

        violations: list[str] = []
        for critic_id, rows in sorted(by_critic.items()):
            configs = sorted({config for config in (_config(run) for run in rows) if config})
            if len(configs) > 1:
                rendered = "; ".join("|".join(config) for config in configs)
                first = rows[0]
                kind = (
                    "judge configuration"
                    if first.critic_type == CriticType.LLM.value
                    else "rule version"
                )
                violations.append(
                    f"critic {critic_id} ran under {len(configs)} different {kind}s "
                    f"({rendered}): its output is not comparable across those runs"
                )
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
            recomputed = entry_hash(
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

        # A finding must reference the claim or event it assesses, or the composer refuses it.
        # C-010's subject is NYX's own store, and the only inspectable anchor available is the
        # event that triggered the run -- so the finding cites that. Without this, a store that
        # genuinely fails self-consistency cannot be reported at all: the composer raises, and
        # because the raise happens outside the registry's per-critic guard, the whole pack run
        # fails with a 500 instead of recording the violation.
        claim_ids = [context.linked_claim.id] if context.linked_claim is not None else []
        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"NYX self-consistency check found {len(violations)} violation(s) in the "
                "stored model."
            ),
            scope=context.scope,
            event_ids=[context.target_event.id],
            claim_ids=claim_ids,
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


def check_finding_consistency(session: Session, finding_id: str) -> list[str]:
    """Run C-010's checks scoped to one stored finding.

    The standalone critic (:meth:`NyxSelfConsistencyCritic.run`) is store-global by
    design: a self-consistency violation *anywhere* undermines trust in every
    confirmed finding. The lifecycle **gate** is narrower, because a gate has to be
    answerable to the caller -- it reports why *this* finding may not be confirmed.
    It runs the same check helpers against one row, plus the audit-chain check,
    which is inherently store-global (a broken chain anywhere breaks the evidence for
    every record). The cross-row checks (duplicate root causes, contradictory
    severities, version drift) need the whole table to mean anything, so a single-row
    call reports nothing for them by construction rather than passing silently.

    Returns the violation list; empty means the finding passes. This is the function
    the lifecycle gate calls, so scenario 24 ("every confirmed finding passes the NYX
    self-consistency critic") is upheld by a code path rather than by a test.
    """

    rows = list(session.scalars(select(FindingRow).where(FindingRow.id == finding_id)))
    if not rows:
        return [f"finding {finding_id} does not exist"]
    row = rows[0]
    runs = list(session.scalars(select(CriticRunRow).order_by(CriticRunRow.id)))
    audits = list(session.scalars(select(AuditRow).order_by(AuditRow.sequence)))
    evidence_ids = set(session.scalars(select(EvidenceRow.id)))
    observation_ids = set(session.scalars(select(ObservationRow.id)))

    violations: list[str] = []
    violations.extend(NyxSelfConsistencyCritic._check_audit_chain(audits))
    violations.extend(NyxSelfConsistencyCritic._check_findings([row], runs, evidence_ids))
    violations.extend(NyxSelfConsistencyCritic._check_observation_refs([row], observation_ids))
    violations.extend(NyxSelfConsistencyCritic._check_transitions([row], audits))
    return violations
