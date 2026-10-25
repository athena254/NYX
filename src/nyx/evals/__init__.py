"""The golden-set layer: reading a spec, drafting cases, and reviewing them.

Three steps, and the order is the point:

1. :mod:`nyx.evals.spec` reads a specification into normative clauses and proposes a verdict
   from each clause's own wording. Deterministic.
2. :mod:`nyx.evals.generate` drafts the *inputs* for each clause through the existing LLM
   seam. Schema-validated, provenance-recorded, fail-closed.
3. :mod:`nyx.evals.review` lets a human approve or reject each case. The only step that
   makes a case scorable.

A case is never scored until step 3, and step 3 is the only step that can be performed by a
person. That is what keeps the golden set evidence about NYX rather than about NYX's own
reading of its own spec — the independence the generator cannot provide for itself.
"""

from __future__ import annotations

from nyx.evals.generate import (
    PROMPT_TEMPLATE_VERSION,
    CaseGenerationError,
    CaseGenerator,
    DraftOutcome,
    build_draft_schema,
    build_prompt,
)
from nyx.evals.review import ReviewError, ReviewQueue
from nyx.evals.schema import (
    CaseEvidence,
    CaseProvenance,
    CaseStatus,
    EvalCase,
    ExpectedVerdict,
)
from nyx.evals.spec import ProposedLabel, SpecClause, parse_spec, propose_label
from nyx.evals.store import (
    CaseEvent,
    CaseEventType,
    CaseLogError,
    append_event,
    current_state,
    load_events,
    pending_cases,
    scorable_cases,
    write_cases,
)

__all__ = [
    "PROMPT_TEMPLATE_VERSION",
    "CaseEvidence",
    "CaseEvent",
    "CaseEventType",
    "CaseGenerationError",
    "CaseLogError",
    "CaseProvenance",
    "CaseStatus",
    "CaseGenerator",
    "DraftOutcome",
    "EvalCase",
    "ExpectedVerdict",
    "ProposedLabel",
    "ReviewError",
    "ReviewQueue",
    "SpecClause",
    "append_event",
    "build_draft_schema",
    "build_prompt",
    "current_state",
    "load_events",
    "parse_spec",
    "pending_cases",
    "propose_label",
    "scorable_cases",
    "write_cases",
]
