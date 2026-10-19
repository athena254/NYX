"""Finding and CriticRun domain models (SPEC §5.5, §5.6, §6).

A finding separates **observations**, **inferences**, **recommendations**, and **unknowns**
into distinct fields. They are never collapsed into one prose field, because a reader must be
able to see which parts are recorded facts, which parts are NYX's reasoning, and which parts
are options for an authorised human.

A ``CriticRun`` records exactly what a critic evaluated, at which version, and with what
result. ``no_finding`` means only that *this* critic found nothing in *its* declared scope; it
is never evidence that the underlying claim is true.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from nyx.domain.base import (
    Digest,
    Identifier,
    NyxModel,
    SchemaVersion,
    Scope,
    UtcDatetime,
    utc_now,
)
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    FindingCategory,
    FindingStatus,
    Severity,
)
from nyx.domain.ids import new_critic_run_id, new_finding_id

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
LimitedText = Annotated[str, StringConstraints(min_length=1, max_length=500)]
RationaleText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class Confidence(NyxModel):
    """An optional assessment aid with an explicit method; never a substitute for evidence."""

    value: float = Field(ge=0.0, le=1.0)
    method: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class Reasoning(NyxModel):
    """Inspectable reasoning with explicit premises and inference.

    ``observations`` are recorded facts or direct readings. ``inferences`` are NYX's
    conclusions drawn from those observations. They are stored separately on purpose.
    """

    observations: list[ShortText] = Field(min_length=1, max_length=64)
    inferences: list[ShortText] = Field(default_factory=list, max_length=64)
    premises: list[ShortText] = Field(default_factory=list, max_length=64)
    contradiction: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    reproduction: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None


class RecommendedAction(NyxModel):
    """A recommendation for an authorised human or policy gate.

    ``is_recommendation`` is pinned to ``True``: the MVP never performs an external action, so
    NYX output can only ever be advice.
    """

    text: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    is_recommendation: Literal[True] = True
    requires_authorization: bool = True
    authority: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None


class CriticRun(NyxModel):
    """A persisted critic execution. Matches the SPEC §5.6 field set."""

    id: Identifier = Field(default_factory=new_critic_run_id)
    critic_id: Identifier
    critic_type: CriticType = CriticType.DETERMINISTIC
    critic_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    rule_version: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    model_version: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    input_refs: list[Identifier] = Field(min_length=1, max_length=256)
    input_hashes: list[Digest] = Field(default_factory=list, max_length=256)
    started_at: UtcDatetime
    completed_at: UtcDatetime
    result: CriticResultStatus
    rationale: RationaleText
    tool_run_refs: list[Annotated[str, StringConstraints(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=64
    )
    resource_usage: dict[str, float] | None = None
    failure_details: RationaleText | None = None
    finding_id: Identifier | None = None

    @model_validator(mode="after")
    def _completed_not_before_started(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        return self


class Finding(NyxModel):
    """A composed finding with explicit evidence links (SPEC §5.5)."""

    id: Identifier = Field(default_factory=new_finding_id)
    schema_version: SchemaVersion = "1.0"
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    category: FindingCategory
    severity: Severity
    confidence: Confidence | None = None
    scope: Scope
    affected_system_ids: list[Identifier] = Field(min_length=1, max_length=64)
    claim_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    event_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    supporting_evidence_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    counterevidence_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    reasoning: Reasoning
    limitations: list[LimitedText] = Field(default_factory=list, max_length=32)
    unknowns: list[LimitedText] = Field(default_factory=list, max_length=32)
    recommended_action: RecommendedAction
    critic_runs: list[CriticRun] = Field(min_length=1, max_length=64)
    status: FindingStatus = FindingStatus.DETECTED
    created_at: UtcDatetime = Field(default_factory=utc_now)
    updated_at: UtcDatetime = Field(default_factory=utc_now)
    dedupe_key: Digest | None = None
    dedupe_rationale: RationaleText | None = None
    duplicate_of: Identifier | None = None

    @model_validator(mode="after")
    def _scope_must_narrow_and_be_consistent(self) -> Self:
        if not self.scope.has_detail():
            raise ValueError(
                "finding scope must narrow beyond the system id (artifact, commit, environment, "
                "period, or conditions)"
            )
        if self.scope.system_id not in self.affected_system_ids:
            raise ValueError("affected_system_ids must include the finding scope system_id")
        referenced = set(self.supporting_evidence_ids) | set(self.counterevidence_ids)
        if not referenced.issubset(set(self.evidence_ids)):
            raise ValueError(
                "supporting_evidence_ids and counterevidence_ids must be a subset of evidence_ids"
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        return self
