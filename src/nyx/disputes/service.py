"""The dispute workflow: rebuttals, verification jobs, dispositions, history (SPEC §5.7, §6).

This service is the only way to move a finding through its dispute states. It never assigns a status
directly: every move goes through :class:`~nyx.findings.lifecycle.FindingLifecycle`, so the graph,
the capability seam, and the audit trail stay the single source of truth.

The append-only contract is enforced here rather than left to convention:

* A rebuttal **appends**. The finding row is never rewritten -- its ``status`` pointer moves and
  its ``counterevidence_ids`` grow, and nothing else about it changes.
* A disposition **supersedes by linking**. The previous disposition row is left exactly as it was.
* A verification job is a record of an attempt. A re-run is a new job.

Design note on the rebuttal path
--------------------------------
The SPEC §6 graph has no direct edge into ``rebuttal_pending``: a finding must pass through
``evidence_pending`` and ``challenged`` first. :meth:`DisputeService.submit_rebuttal` therefore
walks the canonical path rather than jumping, so a finding that has just been raised and one that
is already challenged both end up in the same state by the same route.

Actors
------
Every mutation records *who* performed it. The lifecycle transition's actor is the principal on
whose behalf the service acts, and the rebuttal or disposition separately records the submitter
or decider. Both are required: an anonymous challenge or verdict is not auditable.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from nyx.disputes.models import Disposition, Rebuttal, VerificationJob
from nyx.disputes.verification import (
    VerificationAllowlist,
    VerificationContext,
    VerificationNotPermittedError,
)
from nyx.domain.base import Identifier, PrincipalRef, utc_now
from nyx.domain.enums import (
    DispositionOutcome,
    FindingStatus,
    RebuttalRequestedOutcome,
    VerificationJobStatus,
)
from nyx.error_codes import ErrorCode
from nyx.findings.lifecycle import (
    CapabilityChecker,
    DenyAllCapabilityChecker,
    FindingLifecycle,
    TransitionRequest,
    allowed_transitions,
)
from nyx.findings.models import Finding
from nyx.storage.repositories.dispositions import DispositionRepository
from nyx.storage.repositories.evidence import EvidenceRepository
from nyx.storage.repositories.findings import FindingRepository
from nyx.storage.repositories.rebuttals import RebuttalRepository
from nyx.storage.repositories.verification_jobs import VerificationJobRepository

#: The canonical route a finding travels to reach ``rebuttal_pending`` (SPEC §6).
_REBUTTAL_PATH: tuple[FindingStatus, ...] = (
    FindingStatus.EVIDENCE_PENDING,
    FindingStatus.CHALLENGED,
    FindingStatus.REBUTTAL_PENDING,
)

_OUTCOME_TO_STATUS: dict[DispositionOutcome, FindingStatus] = {
    DispositionOutcome.CONFIRMED: FindingStatus.CONFIRMED,
    DispositionOutcome.PARTIALLY_CONFIRMED: FindingStatus.PARTIALLY_CONFIRMED,
    DispositionOutcome.REJECTED: FindingStatus.REJECTED,
    DispositionOutcome.INCONCLUSIVE: FindingStatus.INCONCLUSIVE,
    DispositionOutcome.ACCEPTED_RISK: FindingStatus.ACCEPTED_RISK,
    DispositionOutcome.DUPLICATE: FindingStatus.DUPLICATE,
    DispositionOutcome.WITHDRAWN: FindingStatus.WITHDRAWN,
}


class DisputeError(Exception):
    """Base class for dispute failures. Carries a stable ``code``."""

    code: ErrorCode

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.detail = message


class FindingNotFoundError(DisputeError):
    """The finding being disputed does not exist."""

    code = ErrorCode.NOT_FOUND


class UnknownEvidenceError(DisputeError):
    """A rebuttal referenced evidence that does not exist."""

    code = ErrorCode.UNKNOWN_EVIDENCE_ID


@dataclass(frozen=True)
class DisputeHistory:
    """Everything recorded about one finding's dispute, in the order it happened."""

    finding: Finding
    rebuttals: list[Rebuttal]
    verification_jobs: list[VerificationJob]
    dispositions: list[Disposition]

    @property
    def current_disposition(self) -> Disposition | None:
        """The disposition nothing supersedes, or ``None`` if there has not been one."""

        return self.dispositions[-1] if self.dispositions else None

    @property
    def chain(self) -> list[Disposition]:
        """The full supersession chain, oldest first."""

        return list(self.dispositions)


class DisputeService:
    """The dispute workflow. Never commits implicitly."""

    def __init__(
        self,
        session: Session,
        *,
        capability_checker: CapabilityChecker | None = None,
        allowlist: VerificationAllowlist | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._lifecycle = FindingLifecycle(
            session, capability_checker=capability_checker or DenyAllCapabilityChecker()
        )
        self._allowlist = allowlist
        self._clock = clock
        self._findings = FindingRepository(session)
        self._rebuttals = RebuttalRepository(session)
        self._dispositions = DispositionRepository(session)
        self._jobs = VerificationJobRepository(session)
        self._evidence = EvidenceRepository(session)

    # -- rebuttals ---------------------------------------------------------

    def submit_rebuttal(
        self,
        *,
        finding_id: Identifier,
        actor: PrincipalRef,
        submitted_by: Identifier,
        argument: str,
        evidence_ids: Sequence[Identifier] = (),
        requested_outcome: RebuttalRequestedOutcome = RebuttalRequestedOutcome.NO_CHANGE,
    ) -> Rebuttal:
        """Record a challenge and advance the finding to ``rebuttal_pending``.

        Every referenced evidence id must exist; an unknown id is refused rather than stored,
        because a rebuttal citing evidence nobody can inspect is not a challenge NYX can
        adjudicate.

        The original finding is never rewritten. Its status pointer advances along the canonical
        path and its ``counterevidence_ids`` grow; its title, summary, reasoning, and evidence
        links are untouched.
        """

        finding = self._require_finding(finding_id)
        self._require_known_evidence(evidence_ids)

        rebuttal = Rebuttal(
            finding_id=finding.id,
            submitted_by=submitted_by,
            submitted_at=self._clock(),
            argument=argument,
            evidence_ids=list(evidence_ids),
            requested_outcome=requested_outcome,
        )
        self._rebuttals.add(rebuttal)
        self._session.flush()

        # Walk the canonical path to rebuttal_pending, so a freshly raised finding and an
        # already-challenged one arrive by the same route. A finding already past rebuttal
        # (verification_pending or later) has no edge back and stays where it is: the rebuttal is
        # still recorded, because a challenge is history even when it cannot reopen the finding.
        for target in _REBUTTAL_PATH:
            if finding.status is target:
                break
            if target not in allowed_transitions(finding.status):
                continue
            self._transition(
                finding,
                target,
                actor=actor,
                rebuttal_id=rebuttal.id,
                counterevidence_ids=(
                    list(evidence_ids) if target is FindingStatus.REBUTTAL_PENDING else []
                ),
                rationale=f"rebuttal {rebuttal.id} submitted by {submitted_by}: {argument}",
            )
            finding = self._require_finding(finding_id)
        return rebuttal

    # -- verification ------------------------------------------------------

    def request_verification(
        self,
        *,
        finding_id: Identifier,
        check_id: str,
        requested_by: Identifier,
    ) -> VerificationJob:
        """Queue an allowlisted verification check for a finding.

        The check is validated against the allowlist here as well as at run time, so an unregistered
        check is refused when it is requested rather than after it has been queued.
        """

        finding = self._require_finding(finding_id)
        if self._allowlist is not None and not self._allowlist.is_allowed(check_id):
            raise VerificationNotPermittedError(
                f"check {check_id!r} is not on the verification allowlist"
            )

        job = VerificationJob(
            finding_id=finding.id,
            check_id=check_id,
            requested_by=requested_by,
            requested_at=self._clock(),
            status=VerificationJobStatus.QUEUED,
        )
        self._jobs.add(job)
        return job

    def run_pending_verification(self, *, limit: int = 100) -> list[VerificationJob]:
        """Run queued jobs through the allowlist, oldest first.

        A job that is refused or that fails is recorded as such; it is never rewritten into a
        success. The finding is not touched -- only an authorised disposition moves it, so a failed
        verification can never look like a passed one.
        """

        if self._allowlist is None:
            return []
        completed: list[VerificationJob] = []
        for job in self._jobs.list_queued(limit=limit):
            finding = self._findings.get(job.finding_id)
            if finding is None:
                continue
            started_at = self._clock()
            context = VerificationContext(session=self._session, finding=finding, clock=self._clock)
            try:
                result = self._allowlist.run(job.check_id, context)
            except Exception as exc:  # noqa: BLE001 - record the failure, never corrupt the job
                job.status = (
                    VerificationJobStatus.REFUSED
                    if isinstance(exc, VerificationNotPermittedError)
                    else VerificationJobStatus.FAILED
                )
                job.failure_details = f"{type(exc).__name__}: {exc}"[:4000]
                job.started_at = started_at
                job.completed_at = self._clock()
                completed.append(job)
                continue

            job.status = VerificationJobStatus.SUCCEEDED
            job.outcome = result.outcome
            job.result_summary = result.result_summary
            job.independence = result.independence
            job.evidence_ids = list(result.evidence_ids)
            job.tool_run_refs = list(result.tool_run_refs)
            job.started_at = started_at
            job.completed_at = self._clock()
            completed.append(job)
        return completed

    # -- dispositions ------------------------------------------------------

    def record_disposition(
        self,
        *,
        finding_id: Identifier,
        actor: PrincipalRef,
        outcome: DispositionOutcome,
        rationale: str,
        decided_by: Identifier,
        policy_version: str,
        verification_run_ids: Sequence[Identifier] = (),
        counterevidence_ids: Sequence[Identifier] = (),
        rebuttal_id: Identifier | None = None,
    ) -> Disposition:
        """Record an authorised outcome and move the finding to the matching state.

        Capability is enforced by the lifecycle machine rather than here, so a principal without the
        disposition capability cannot record one. The disposition supersedes the previous one by
        linking to it instead of replacing it.

        ``rebuttal_id`` defaults to the finding's latest rebuttal: a disposition is normally an
        answer to a challenge, and recording which challenge it answers makes history legible.
        """

        finding = self._require_finding(finding_id)
        target = _OUTCOME_TO_STATUS[outcome]

        if rebuttal_id is None:
            rebuttals = self._rebuttals.list(finding_id=finding.id)
            if rebuttals:
                rebuttal_id = rebuttals[-1].id

        previous = self._dispositions.latest(finding.id)
        self._transition(
            finding,
            target,
            actor=actor,
            rebuttal_id=rebuttal_id,
            counterevidence_ids=list(counterevidence_ids),
            verification_job_ids=list(verification_run_ids),
            policy_version=policy_version,
            rationale=rationale,
        )

        disposition = Disposition(
            finding_id=finding.id,
            outcome=outcome,
            rationale=rationale,
            decided_by=decided_by,
            decided_at=self._clock(),
            verification_run_ids=list(verification_run_ids),
            policy_version=policy_version,
            supersedes_disposition_id=None if previous is None else previous.id,
        )
        self._dispositions.add(disposition)
        return disposition

    # -- history -----------------------------------------------------------

    def get_history(self, *, finding_id: Identifier) -> DisputeHistory:
        """Return everything recorded about a finding's dispute.

        Nothing is ever removed from the result: the original finding, every rebuttal, every
        verification attempt, and the full disposition chain are all present.
        """

        finding = self._require_finding(finding_id)
        return DisputeHistory(
            finding=finding,
            rebuttals=list(self._rebuttals.list(finding_id=finding.id)),
            verification_jobs=list(self._jobs.list(finding_id=finding.id)),
            dispositions=list(self._dispositions.list(finding_id=finding.id)),
        )

    def disposition_chain(self, *, disposition_id: Identifier) -> list[Disposition]:
        """Return one disposition's full supersession chain, oldest first."""

        return list(self._dispositions.chain(disposition_id))

    # -- internals ---------------------------------------------------------

    def _require_finding(self, finding_id: Identifier) -> Finding:
        finding = self._findings.get(finding_id)
        if finding is None:
            raise FindingNotFoundError(f"finding {finding_id!r} does not exist")
        return finding

    def _require_known_evidence(self, evidence_ids: Sequence[Identifier]) -> None:
        for evidence_id in evidence_ids:
            if self._evidence.get(evidence_id) is None:
                raise UnknownEvidenceError(
                    f"rebuttal references unknown evidence {evidence_id!r}; a challenge must cite "
                    "evidence that can be inspected"
                )

    def _transition(
        self,
        finding: Finding,
        target: FindingStatus,
        *,
        actor: PrincipalRef,
        rationale: str,
        rebuttal_id: Identifier | None = None,
        counterevidence_ids: Sequence[Identifier] = (),
        verification_job_ids: Sequence[Identifier] = (),
        policy_version: str | None = None,
    ) -> None:
        self._lifecycle.transition(
            TransitionRequest(
                finding_id=finding.id,
                target_status=target,
                actor=actor,
                request_id=f"req-{self._clock().isoformat()}",
                rationale=rationale,
                rebuttal_id=rebuttal_id,
                counterevidence_ids=list(counterevidence_ids),
                verification_job_ids=list(verification_job_ids),
                policy_version=policy_version,
            )
        )
