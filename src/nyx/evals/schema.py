"""The eval-case schema — a labelled input a critic pack can be run against.

An eval case is the unit the golden set is made of. It is deliberately the *same shape* as
``evals/llm-critic/corpus.jsonl``, because that file already defines what the eval runner
can consume (``scripts/eval_llm_critic.py::seed_case``). A second, subtly different case
shape is how the generator's output silently stops being runnable, so the generator emits
this shape and nothing else.

The separation that matters is between the three things a case can be:

* a **draft** — proposed, unlabelled-as-final, not yet part of the golden set;
* **approved** — a human has confirmed both the inputs and the label, and it counts;
* **rejected** — a human has looked at it and refused it, and it must never count.

A draft is never scored. That is not a workflow nicety: a case whose *label* came from the
same source as its inputs is the source checking itself, and scoring it proves nothing. The
review step is what makes a case evidence about NYX rather than about NYX's own reading.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from nyx.domain.base import NyxModel, SchemaVersion, UtcDatetime, utc_now
from nyx.domain.enums import EvidenceKind, IntegrityMethod, RetentionClass, Sensitivity

CaseId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
ClauseRef = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class CaseStatus(StrEnum):
    """Where a generated case sits in its lifecycle.

    ``DRAFT`` is the state every generated case starts in and the only state the generator
    can produce. ``APPROVED`` is the only state the golden set is scored from. ``REJECTED``
    records that a human looked at the case and refused it, which is a fact worth keeping:
    a case rejected for a good reason is a hint about the spec, and silently dropping it
    loses that.
    """

    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class ExpectedVerdict(StrEnum):
    """The outcome a critic pack is expected to reach on this case.

    The three values are the families ``scripts/eval_llm_critic.py`` already scores, so a
    case can be handed to the existing runner without a mapping layer.
    """

    NONE = "none"
    FINDING = "finding"
    INCONCLUSIVE = "inconclusive"


class CaseEvidence(NyxModel):
    """One evidence item in a case.

    A subset of :class:`nyx.domain.evidence.EvidenceItem` — the fields a *case* needs, which
    is what the eval runner reads before it fills in the rest (source ref, relevance target,
    producer id) from the case itself. Modelling the real enums rather than free strings means
    a generated case that names a nonexistent evidence kind is refused here, at the boundary,
    instead of failing halfway through a seeded run.

    ``captured_at`` and ``valid_from`` are **required**, not defaulted. They were optional
    once, and the first exported golden case failed to seed with
    ``SCHEMA_VALIDATION_FAILED`` on exactly those two fields — the export wrote a case the
    rest of NYX could not consume, which is the failure this repository has been bitten by
    before. A case without a capture time is not a case; it is a description of one.
    """

    kind: EvidenceKind
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    scope: dict[str, Any] | None = None
    captured_at: UtcDatetime
    valid_from: UtcDatetime
    valid_until: UtcDatetime | None = None
    integrity_method: IntegrityMethod = IntegrityMethod.NONE
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    access_policy_ref: Annotated[str, StringConstraints(min_length=1, max_length=256)] = "eval"
    retention_class: RetentionClass = RetentionClass.STANDARD


class CaseProvenance(NyxModel):
    """Where a case came from, and who has touched it since.

    The provenance is the audit trail that makes a case reviewable. ``label_source`` is the
    field that matters most: it records *where the expected verdict came from*, and the only
    two honest values are ``deterministic_rule`` (derived from the clause's own normative
    wording) and ``human_review`` (a reviewer set or corrected it). A case whose label has
    no recorded source is a case nobody can defend.
    """

    #: Which model drafted the inputs, when the generator used one. ``None`` for a case a
    #: human wrote, and for a case produced with no provider configured.
    model_provider: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    model_version: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    prompt_version: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    #: The spec clause this case was derived from, quoted. A case with no clause is not
    #: traceable to the contract it is supposed to test.
    spec_ref: ClauseRef | None = None
    spec_revision: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    #: ``deterministic_rule`` or ``human_review``. See the class docstring.
    label_source: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    generated_at: UtcDatetime = Field(default_factory=utc_now)
    #: Set when a reviewer confirms or corrects the proposed label. Empty until then: a case
    #: with no reviewer has not been reviewed, whatever its inputs look like.
    reviewed_by: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    reviewed_at: UtcDatetime | None = None
    review_note: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None


class EvalCase(NyxModel):
    """One labelled eval case: inputs, an expected verdict, and its provenance."""

    case_id: CaseId
    schema_version: SchemaVersion = "1.0"
    #: Free-text description of the spec requirement this case exercises.
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    #: Where the case came from, in the corpus's own loose sense: a spec section, an
    #: acceptance scenario number, or a historical defect id.
    source: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    spec_ref: ClauseRef
    #: The verdict the deterministic labelling rule proposed from the clause. It is *proposed*,
    #: not settled, until a reviewer approves it — see :class:`CaseStatus`.
    expected: ExpectedVerdict
    claim: dict[str, Any]
    event: dict[str, Any]
    evidence: list[CaseEvidence] = Field(default_factory=list, max_length=64)
    status: CaseStatus = CaseStatus.DRAFT
    provenance: CaseProvenance = Field(default_factory=CaseProvenance)
    notes: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None

    @property
    def is_scorable(self) -> bool:
        """Whether this case may be scored.

        Only an approved case counts. A draft that happens to have a plausible label is still
        not evidence, and a rejected case must never be scored no matter how correct it looks.
        """

        return self.status is CaseStatus.APPROVED
