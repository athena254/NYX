"""Finding lifecycle state machine (SPEC §6).

SPEC §6 defines the finding lifecycle as a *validated* state machine: the legal
transitions are a fixed graph, and several transitions carry safety rules the
machine itself must enforce. This module owns that graph and those rules.

Design notes
------------
* The transition graph is *data* (``_TRANSITIONS``), not a chain of ``if``
  statements: legality of a move is a set-membership check, so the whole graph is
  reviewable at a glance and a property test can fuzz it exhaustively.
* The safety rules (SPEC §3.2 invariant 1 and the §6 rules) are declarative
  per-target validators registered in ``_RULES`` / ``_EDGE_RULES``; the machine
  runs them only after the graph check and refuses *before* touching state, so a
  refused transition can never mutate the stored record.
* Authorization is a seam, not an implementation. ``CapabilityChecker`` is the
  protocol Phase 4 wires to real authentication; the default
  ``DenyAllCapabilityChecker`` is explicit and safe -- no capability is granted
  unless a real checker is injected, so no disposition can change or risk be
  accepted by default.
* Every accepted transition appends an audit entry via the existing
  ``nyx.audit.writer.AuditWriter``. Per ADR 0003 the history lives in the
  append-only audit log; the finding row's ``status`` column is only the
  current-state pointer derived from the latest valid transition.
* Capability is checked before the graph so an unauthorized principal cannot
  probe which transitions exist (fail closed).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import StringConstraints
from sqlalchemy.orm import Session

from nyx.audit.writer import AuditEntryInput, AuditWriter
from nyx.domain.base import Identifier, NyxModel, PrincipalRef, utc_now
from nyx.domain.enums import AuditOutcome, FindingStatus, VerificationResult
from nyx.error_codes import ErrorCode
from nyx.findings.models import Finding, RationaleText
from nyx.storage.repositories.findings import FindingRepository
from nyx.storage.tables import FindingRow

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LifecycleError(Exception):
    """Base class for finding lifecycle failures.

    Carries a stable ``code`` from the shared error-code catalogue so the API
    and CLI layers can map failures without importing lifecycle internals.
    """

    code: ErrorCode

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.detail = message


class InvalidLifecycleTransitionError(LifecycleError):
    """The requested transition is not in the graph or violates a safety rule."""

    code = ErrorCode.INVALID_LIFECYCLE_TRANSITION


class CapabilityDeniedError(LifecycleError):
    """The actor lacks the capability required for this transition."""

    code = ErrorCode.CAPABILITY_REQUIRED


class FindingNotFoundError(LifecycleError):
    """The finding does not exist."""

    code = ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# Capability seam (Phase 4 wires real authentication here)
# ---------------------------------------------------------------------------


class Capability(StrEnum):
    """The capabilities the lifecycle machine asks about.

    ``DISPOSITION`` covers every change of a finding's disposition (the four
    verdicts, remediation, and withdrawal, which SPEC §5.7 lists as a
    disposition outcome). ``ACCEPT_RISK`` is separate because accepting risk is
    a distinct authorisation (SPEC §6). ``ADMINISTER`` covers the purely
    administrative terminal states.
    """

    DISPOSITION = "findings.disposition"
    ACCEPT_RISK = "findings.accept_risk"
    ADMINISTER = "findings.administer"


class CapabilityChecker(Protocol):
    """The authorization seam for disposition changes and risk acceptance.

    The lifecycle machine never decides authorization itself; it only asks the
    injected checker. Phase 4 owns real authentication and supplies the
    implementation.
    """

    def has_capability(self, principal: PrincipalRef, capability: Capability) -> bool:
        """Return whether ``principal`` holds ``capability``."""
        ...


class DenyAllCapabilityChecker:
    """Default checker: grants nothing.

    Explicit and safe. Until Phase 4 wires a real checker, no principal may
    change a disposition or accept risk. Ordinary progress transitions require
    no capability and are unaffected by this default.
    """

    def has_capability(self, principal: PrincipalRef, capability: Capability) -> bool:
        return False


# ---------------------------------------------------------------------------
# Transition request
# ---------------------------------------------------------------------------


class ClosureWaiver(NyxModel):
    """An explicitly authorised waiver of the verification requirement for closure.

    SPEC §6: closure requires a verification result *or* an explicitly
    authorised waiver. The authoriser and reason are recorded so the waiver is
    never silent.
    """

    authorised_by: Identifier
    reason: RationaleText


class TransitionRequest(NyxModel):
    """One requested lifecycle transition.

    ``rationale`` is the recorded reason for the move; the per-target rules in
    ``_RULES`` require it where SPEC §6 does (confirmed, rejected, and the
    other disposition outcomes). ``verification_result`` / ``waiver`` are
    closure evidence; ``rebuttal_bypass_policy`` is the documented policy that
    permits skipping rebuttal; ``duplicate_of`` names the original finding.
    """

    finding_id: Identifier
    target_status: FindingStatus
    actor: PrincipalRef
    request_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    rationale: RationaleText | None = None
    verification_result: VerificationResult | None = None
    waiver: ClosureWaiver | None = None
    rebuttal_bypass_policy: (
        Annotated[str, StringConstraints(min_length=1, max_length=256)] | None
    ) = None
    duplicate_of: Identifier | None = None


# ---------------------------------------------------------------------------
# The validated transition graph (SPEC §6)
# ---------------------------------------------------------------------------

# Administrative/terminal states are reachable from every non-terminal state:
# a finding can be withdrawn, duplicated, expired, or have risk accepted at any
# point before closure. The five terminal states have no outgoing edges.
_TRANSITIONS: dict[FindingStatus, frozenset[FindingStatus]] = {
    FindingStatus.DETECTED: frozenset(
        {
            FindingStatus.EVIDENCE_PENDING,
            FindingStatus.ACCEPTED_RISK,
            FindingStatus.DUPLICATE,
            FindingStatus.WITHDRAWN,
            FindingStatus.EXPIRED,
        }
    ),
    FindingStatus.EVIDENCE_PENDING: frozenset(
        {
            FindingStatus.CHALLENGED,
            FindingStatus.ACCEPTED_RISK,
            FindingStatus.DUPLICATE,
            FindingStatus.WITHDRAWN,
            FindingStatus.EXPIRED,
        }
    ),
    FindingStatus.CHALLENGED: frozenset(
        {
            FindingStatus.REBUTTAL_PENDING,
            # Skipping rebuttal is allowed only under a documented policy; the
            # bypass is validated (and must be recorded) by the edge rule.
            FindingStatus.VERIFICATION_PENDING,
            FindingStatus.ACCEPTED_RISK,
            FindingStatus.WITHDRAWN,
            FindingStatus.EXPIRED,
        }
    ),
    FindingStatus.REBUTTAL_PENDING: frozenset(
        {
            FindingStatus.VERIFICATION_PENDING,
            FindingStatus.ACCEPTED_RISK,
            FindingStatus.WITHDRAWN,
            FindingStatus.EXPIRED,
        }
    ),
    FindingStatus.VERIFICATION_PENDING: frozenset(
        {
            FindingStatus.CONFIRMED,
            FindingStatus.PARTIALLY_CONFIRMED,
            FindingStatus.REJECTED,
            FindingStatus.INCONCLUSIVE,
            FindingStatus.ACCEPTED_RISK,
            FindingStatus.WITHDRAWN,
            FindingStatus.EXPIRED,
        }
    ),
    FindingStatus.CONFIRMED: frozenset(
        {
            FindingStatus.REMEDIATION_PENDING,
            FindingStatus.ACCEPTED_RISK,
            FindingStatus.WITHDRAWN,
            FindingStatus.EXPIRED,
        }
    ),
    FindingStatus.PARTIALLY_CONFIRMED: frozenset(
        {
            FindingStatus.REMEDIATION_PENDING,
            FindingStatus.ACCEPTED_RISK,
            FindingStatus.WITHDRAWN,
            FindingStatus.EXPIRED,
        }
    ),
    FindingStatus.REJECTED: frozenset(
        {
            FindingStatus.REMEDIATION_PENDING,
            FindingStatus.ACCEPTED_RISK,
            FindingStatus.WITHDRAWN,
            FindingStatus.EXPIRED,
        }
    ),
    # inconclusive has no edge to any verdict: it must never be silently
    # converted into a positive or negative outcome (SPEC §6).
    FindingStatus.INCONCLUSIVE: frozenset(
        {
            FindingStatus.REMEDIATION_PENDING,
            FindingStatus.ACCEPTED_RISK,
            FindingStatus.WITHDRAWN,
            FindingStatus.EXPIRED,
        }
    ),
    FindingStatus.REMEDIATION_PENDING: frozenset(
        {
            FindingStatus.FIXED_PENDING_VERIFICATION,
            FindingStatus.ACCEPTED_RISK,
            FindingStatus.WITHDRAWN,
            FindingStatus.EXPIRED,
        }
    ),
    FindingStatus.FIXED_PENDING_VERIFICATION: frozenset(
        {
            FindingStatus.CLOSED,
            FindingStatus.ACCEPTED_RISK,
            FindingStatus.WITHDRAWN,
            FindingStatus.EXPIRED,
        }
    ),
    FindingStatus.CLOSED: frozenset(),
    FindingStatus.ACCEPTED_RISK: frozenset(),
    FindingStatus.DUPLICATE: frozenset(),
    FindingStatus.WITHDRAWN: frozenset(),
    FindingStatus.EXPIRED: frozenset(),
}

# Which capability each guarded target requires. Ordinary progress transitions
# (detected -> evidence_pending -> challenged -> rebuttal_pending ->
# verification_pending, and fixed_pending_verification -> closed) require none.
_CAPABILITY_REQUIREMENTS: dict[FindingStatus, Capability] = {
    FindingStatus.CONFIRMED: Capability.DISPOSITION,
    FindingStatus.PARTIALLY_CONFIRMED: Capability.DISPOSITION,
    FindingStatus.REJECTED: Capability.DISPOSITION,
    FindingStatus.INCONCLUSIVE: Capability.DISPOSITION,
    FindingStatus.REMEDIATION_PENDING: Capability.DISPOSITION,
    FindingStatus.WITHDRAWN: Capability.DISPOSITION,
    FindingStatus.ACCEPTED_RISK: Capability.ACCEPT_RISK,
    FindingStatus.DUPLICATE: Capability.ADMINISTER,
    FindingStatus.EXPIRED: Capability.ADMINISTER,
}

TransitionRule = Callable[[Finding, TransitionRequest], None]


def _requires_rationale(finding: Finding, request: TransitionRequest) -> None:
    """A disposition outcome must record why it was reached."""
    if not request.rationale:
        raise InvalidLifecycleTransitionError(
            f"a transition to '{request.target_status.value}' requires a recorded rationale"
        )


def _requires_supporting_evidence(finding: Finding, request: TransitionRequest) -> None:
    """Safety invariant 1 (SPEC §3.2): confirmed/partially_confirmed need evidence.

    Shared by both positive verdicts: a partial confirmation is still a positive
    verdict, so it must meet the same evidence bar. The policy-only exception
    in invariant 1 is not implemented because the Finding model carries no
    policy reference fields; every confirmed finding requires rationale +
    evidence.
    """
    if not finding.supporting_evidence_ids:
        raise InvalidLifecycleTransitionError(
            f"safety invariant 1: a finding may not be moved to "
            f"'{request.target_status.value}' without at least one relevant "
            "supporting evidence item"
        )


def _requires_duplicate_target(finding: Finding, request: TransitionRequest) -> None:
    if not request.duplicate_of:
        raise InvalidLifecycleTransitionError(
            "a transition to 'duplicate' must name the original finding via duplicate_of"
        )


def _closed_requires_verification_or_waiver(finding: Finding, request: TransitionRequest) -> None:
    """SPEC §6: closure requires a verification result or an authorised waiver."""
    if request.verification_result is None and request.waiver is None:
        raise InvalidLifecycleTransitionError(
            "'fixed_pending_verification' is not 'closed': closure requires a "
            "recorded verification result or an explicitly authorised waiver"
        )


def _bypass_requires_documented_policy(finding: Finding, request: TransitionRequest) -> None:
    """SPEC §6: skipping rebuttal is allowed only under a documented policy.

    The bypass must be recorded, not silent: the policy name travels in the
    audit entry's reason.
    """
    if not request.rebuttal_bypass_policy:
        raise InvalidLifecycleTransitionError(
            "skipping rebuttal (challenged -> verification_pending) is allowed "
            "only under a documented policy; the bypass must be recorded"
        )


_RULES: dict[FindingStatus, tuple[TransitionRule, ...]] = {
    FindingStatus.CONFIRMED: (_requires_rationale, _requires_supporting_evidence),
    FindingStatus.PARTIALLY_CONFIRMED: (_requires_rationale, _requires_supporting_evidence),
    # SPEC §6: rejected requires an explanation of which premise failed or what
    # counterevidence prevailed -- enforced as a mandatory recorded rationale.
    FindingStatus.REJECTED: (_requires_rationale,),
    FindingStatus.REMEDIATION_PENDING: (_requires_rationale,),
    FindingStatus.WITHDRAWN: (_requires_rationale,),
    FindingStatus.ACCEPTED_RISK: (_requires_rationale,),
    FindingStatus.DUPLICATE: (_requires_rationale, _requires_duplicate_target),
    FindingStatus.EXPIRED: (_requires_rationale,),
    FindingStatus.CLOSED: (_closed_requires_verification_or_waiver,),
}

_EDGE_RULES: dict[tuple[FindingStatus, FindingStatus], tuple[TransitionRule, ...]] = {
    (FindingStatus.CHALLENGED, FindingStatus.VERIFICATION_PENDING): (
        _bypass_requires_documented_policy,
    ),
}


def allowed_transitions(status: FindingStatus) -> frozenset[FindingStatus]:
    """Return the states a finding in ``status`` may move to."""
    return _TRANSITIONS.get(status, frozenset())


def validate_transition(
    finding: Finding,
    request: TransitionRequest,
    checker: CapabilityChecker,
) -> None:
    """Validate a transition request against the graph and the §6 rules.

    Raises :class:`CapabilityDeniedError` or
    :class:`InvalidLifecycleTransitionError` before any state is touched.
    """
    target = request.target_status

    # Fail closed: check authorization first so an unauthorized principal
    # cannot probe which transitions exist.
    required = _CAPABILITY_REQUIREMENTS.get(target)
    if required is not None and not checker.has_capability(request.actor, required):
        raise CapabilityDeniedError(
            f"principal '{request.actor.id}' lacks capability '{required.value}' "
            f"required to move a finding to '{target.value}'"
        )

    if target not in _TRANSITIONS.get(finding.status, frozenset()):
        raise InvalidLifecycleTransitionError(
            f"a finding in state '{finding.status.value}' may not transition to '{target.value}'"
        )

    for rule in _EDGE_RULES.get((finding.status, target), ()):
        rule(finding, request)
    for rule in _RULES.get(target, ()):
        rule(finding, request)


def _audit_reason(
    previous: FindingStatus,
    request: TransitionRequest,
) -> str:
    """Compose the audit reason, recording every non-obvious fact about the move."""
    parts = [f"lifecycle transition {previous.value} -> {request.target_status.value}"]
    if request.rationale:
        parts.append(request.rationale)
    if request.rebuttal_bypass_policy:
        parts.append(
            f"rebuttal bypassed under documented policy '{request.rebuttal_bypass_policy}'"
        )
    if request.verification_result is not None:
        parts.append(f"verification result: {request.verification_result.value}")
    if request.waiver is not None:
        parts.append(
            f"closure waiver authorised by '{request.waiver.authorised_by}': "
            f"{request.waiver.reason}"
        )
    return "; ".join(parts)[:2000]


class FindingLifecycle:
    """Apply validated lifecycle transitions to stored findings.

    Owns the full path of an accepted transition: load, authorize, validate,
    mutate the current-state row, and append the audit entry. Validation runs
    before any mutation, so a refused transition never touches stored state
    (SPEC §6).
    """

    def __init__(
        self,
        session: Session,
        *,
        capability_checker: CapabilityChecker | None = None,
        audit_writer: AuditWriter | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._capability_checker = capability_checker or DenyAllCapabilityChecker()
        self._audit_writer = audit_writer or AuditWriter(session)
        self._clock = clock
        self._findings = FindingRepository(session)

    def transition(self, request: TransitionRequest) -> Finding:
        """Validate and apply one transition, returning the updated finding.

        Raises :class:`FindingNotFoundError` if the finding does not exist and
        :class:`InvalidLifecycleTransitionError` /
        :class:`CapabilityDeniedError` when the move is refused; in both refusal
        cases the stored state is left unchanged.
        """
        finding = self._findings.get(request.finding_id)
        if finding is None:
            raise FindingNotFoundError(f"finding '{request.finding_id}' does not exist")

        validate_transition(finding, request, self._capability_checker)

        previous = finding.status
        now = self._clock()
        finding.status = request.target_status
        finding.updated_at = now

        row = self._session.get(FindingRow, finding.id)
        assert row is not None  # nosec B101 -- genuine invariant: loaded above
        row.status = request.target_status.value
        row.updated_at = now
        if request.duplicate_of is not None:
            row.duplicate_of = request.duplicate_of
        self._session.flush()

        self._audit_writer.append(
            AuditEntryInput(
                actor=request.actor,
                action="finding.transition",
                target_type="finding",
                target_id=finding.id,
                request_id=request.request_id,
                outcome=AuditOutcome.SUCCESS,
                reason=_audit_reason(previous, request),
            )
        )
        return finding
