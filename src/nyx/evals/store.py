"""The golden-set store: one append-only log of case events.

The store is a JSONL event log rather than a mutable table, and that is a deliberate match to
how the rest of NYX records history: a case is *proposed*, a reviewer *approves* or
*rejects* it, and the current state is **derived by replaying the log** rather than stored as
a pointer that gets overwritten. The log in ``docs/SPEC.md`` §3.2 invariant 5 is the reason —
history is appended to, never rewritten, so a case whose label was corrected keeps both the
correction and the thing it corrected.

Three event kinds, and no update or delete:

* ``case.proposed``  — a generator drafted a case. Carries the whole case.
* ``case.approved``  — a reviewer confirmed the inputs *and* the label. The case becomes
  scorable.
* ``case.rejected``  — a reviewer refused it. It never becomes scorable, and the reason is
  kept: a case rejected for a good reason says something about the spec.

Because the log is the only source of truth, :func:`scorable_cases` is the function that
decides what the golden set *is*. Nothing else may decide that, or two readers would
disagree about what is in the set.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from nyx.domain.base import NyxModel, UtcDatetime
from nyx.evals.schema import CaseId, CaseStatus, EvalCase


class CaseEventType(StrEnum):
    PROPOSED = "case.proposed"
    APPROVED = "case.approved"
    REJECTED = "case.rejected"


class CaseEvent(NyxModel):
    """One entry in the case log."""

    event: CaseEventType
    at: UtcDatetime
    case_id: CaseId
    #: The full case, present only on ``case.proposed``. Later events reference it by id, so
    #: the log does not carry a second, drifting copy of the inputs.
    case: EvalCase | None = None
    reviewed_by: str | None = None
    note: str | None = None


class CaseLogError(Exception):
    """The case log could not be read or written."""


def append_event(handle: TextIO, event: CaseEvent) -> None:
    """Append one event as a single JSON line.

    A line is written atomically enough for a local file (one ``write`` of one serialised
    document) and the file is opened by the caller, so a caller can hold it open for a batch
    of proposals without re-reading between them.
    """

    handle.write(json.dumps(event.model_dump(mode="json")) + "\n")


def load_events(path: Path) -> list[CaseEvent]:
    """Read a case log in order.

    A blank line is skipped rather than fatal — an editor or a interrupted write can leave
    one, and a blank line is not a claim about any case. A line that is not valid JSON, or
    that is not a valid event, *is* fatal: a corrupted log is a reason to stop, because
    silently dropping an event is how a rejected case becomes scorable.
    """

    if not path.exists():
        return []
    events: list[CaseEvent] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(CaseEvent.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise CaseLogError(f"{path} line {number} is not a valid case event: {exc}") from exc
    return events


def current_state(events: list[CaseEvent]) -> dict[str, EvalCase]:
    """Replay the log into the latest state of each case.

    The latest event wins, exactly as the lifecycle derives a finding's status from its
    latest valid transition. A case that was proposed and never reviewed is present and
    still a draft; a case proposed and then rejected is present and rejected.
    """

    state: dict[str, EvalCase] = {}
    for event in events:
        if event.event is CaseEventType.PROPOSED:
            if event.case is None:
                raise CaseLogError(
                    f"event for {event.case_id} is {event.event.value} but carries no case"
                )
            if event.case_id in state:
                # A case id identifies one case. A second proposal for the same id would
                # silently revert a reviewed case to a draft, so it is refused rather than
                # applied — a re-run must pick a fresh id, not overwrite a decision.
                raise CaseLogError(
                    f"{event.case_id} is proposed twice; a case id identifies one case and "
                    "re-proposing would revert its review"
                )
            state[event.case_id] = event.case
            continue

        case = state.get(event.case_id)
        if case is None:
            # A decision about a case that was never proposed is not a decision. Keeping it
            # would let a reviewer approve an id nobody drafted.
            raise CaseLogError(f"{event.event.value} for {event.case_id}, which was never proposed")
        target = (
            CaseStatus.APPROVED if event.event is CaseEventType.APPROVED else CaseStatus.REJECTED
        )
        case.status = target
        case.provenance.reviewed_by = event.reviewed_by
        case.provenance.reviewed_at = event.at
        case.provenance.review_note = event.note
        if event.event is CaseEventType.APPROVED:
            # The label is the reviewer's to confirm or correct. Approval without recording
            # that a human set it would leave the deterministic rule claiming credit for a
            # label a person chose.
            case.provenance.label_source = "human_review"
    return state


def scorable_cases(events: list[CaseEvent]) -> list[EvalCase]:
    """Return the cases that may be scored, in case-id order.

    Only an approved case is scorable. This is the single function that defines the golden
    set; nothing else may decide it.
    """

    return sorted(
        (case for case in current_state(events).values() if case.is_scorable),
        key=lambda case: case.case_id,
    )


def pending_cases(events: list[CaseEvent]) -> list[EvalCase]:
    """Return the cases awaiting review, in case-id order."""

    return sorted(
        (case for case in current_state(events).values() if case.status is CaseStatus.DRAFT),
        key=lambda case: case.case_id,
    )


def write_cases(path: Path, cases: list[EvalCase]) -> None:
    """Write a plain case list, as the eval runner consumes it.

    This is a *derived* view, not the log: it exists so ``scripts/eval_llm_critic.py`` can
    read the golden set without learning the event format. It is regenerated from the log and
    never edited by hand; a hand-edited case here is a case that is not in the log, and the
    log is what the review queue reads.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "case_id": case.case_id,
            "source": case.source,
            "spec_ref": case.spec_ref,
            "expected": case.expected.value,
            "claim": case.claim,
            "event": case.event,
            "evidence": [item.model_dump(mode="json", exclude_none=True) for item in case.evidence],
        }
        for case in cases
    ]
    path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
