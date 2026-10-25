"""The human review queue: what turns a drafted case into a golden case.

A generated case is a *proposal*. It becomes evidence about NYX only when a person who was
not the generator confirms two things: that the inputs really do exercise the requirement,
and that the proposed verdict is the right one.

That second confirmation is the part that cannot be automated, and it is worth being blunt
about why. The proposed label is read from the requirement's own wording by a deterministic
keyword rule. The requirement is also the source of the case's inputs. So the rule is
grading text it was derived from, and the only thing standing between that and a number
that means nothing is a reviewer who checks the label against the requirement and disagrees
when it is wrong.

The queue is therefore strict in three ways:

* **The reviewer's identity is required and never defaulted.** A review with no named
  reviewer is not a review; NYX refuses rather than recording ``"system"``.
* **A decision about an unknown case is refused.** Approving an id nobody drafted would let
  a typo create a golden case.
* **Decisions are appended, never rewritten.** A case reviewed twice keeps both reviews, and
  the latest is derived — the same rule the lifecycle applies to a finding's status.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nyx.domain.base import UtcDatetime, utc_now
from nyx.evals.schema import CaseStatus, EvalCase
from nyx.evals.store import (
    CaseEvent,
    CaseEventType,
    CaseLogError,
    append_event,
    current_state,
    load_events,
    pending_cases,
)


class ReviewError(Exception):
    """A review could not be recorded. Carries a stable ``code``."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ReviewQueue:
    """The queue of cases awaiting review, and the act of reviewing one."""

    def __init__(
        self,
        path: Path,
        *,
        actor: str,
        clock: Callable[[], UtcDatetime] = utc_now,
    ) -> None:
        """Open the queue over a case log.

        ``actor`` is required. This is a standing rule in NYX: never hardcode an actor, and
        refuse to default one, because an entry with no named actor is an entry nobody can
        be asked about.
        """

        if not actor.strip():
            raise ReviewError(
                "a review must name its reviewer; refusing to default an actor",
                code="no_reviewer",
            )
        self._path = path
        self._actor = actor.strip()
        self._clock = clock

    @property
    def actor(self) -> str:
        """The reviewer this queue records decisions for."""

        return self._actor

    def pending(self) -> list[EvalCase]:
        """The cases awaiting review, in case-id order."""

        return pending_cases(self._read())

    def _read(self) -> list[CaseEvent]:
        try:
            return load_events(self._path)
        except CaseLogError as exc:
            raise ReviewError(f"malformed case log: {exc}", code="malformed_log") from exc

    def _decide(
        self,
        case_id: str,
        *,
        decision: CaseEventType,
        note: str | None,
    ) -> CaseEvent:
        events = self._read()
        state = current_state(events)
        case = state.get(case_id)
        if case is None:
            raise ReviewError(
                f"{case_id!r} is not in the case log; a reviewer cannot decide a case that "
                "was never drafted",
                code="unknown_case",
            )
        if case.status is not CaseStatus.DRAFT:
            raise ReviewError(
                f"{case_id!r} is already {case.status.value}; a second decision is recorded "
                "by appending, so re-review it with a corrected note rather than overwriting",
                code="already_decided",
            )
        event = CaseEvent(
            event=decision,
            at=self._clock(),
            case_id=case_id,
            reviewed_by=self._actor,
            note=note,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            append_event(handle, event)
        return event

    def approve(self, case_id: str, *, note: str | None = None) -> CaseEvent:
        """Approve a case, confirming both its inputs and its proposed label.

        The approved case's ``label_source`` becomes ``human_review``, because the label is
        now the reviewer's, not the keyword rule's.
        """

        return self._decide(case_id, decision=CaseEventType.APPROVED, note=note)

    def reject(self, case_id: str, *, note: str | None = None) -> CaseEvent:
        """Reject a case.

        A rejection note is not optional in spirit: a rejected case is the only record that
        a requirement was hard to turn into a test, and a rejection without a reason is a
        case silently dropped.
        """

        if not note or not note.strip():
            raise ReviewError(
                "a rejection must say why; an unexplained rejection is a case silently dropped",
                code="rejection_needs_reason",
            )
        return self._decide(case_id, decision=CaseEventType.REJECTED, note=note)
