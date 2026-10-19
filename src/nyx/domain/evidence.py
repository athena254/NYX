"""The evidence aggregate (SPEC §5.4).

An integrity method such as ``hash`` or ``signed`` establishes integrity or authenticity under
stated assumptions. It never establishes that the statement in the record is true.
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from nyx.domain.base import (
    Digest,
    Identifier,
    NyxModel,
    PrincipalRef,
    SchemaVersion,
    Scope,
    UtcDatetime,
)
from nyx.domain.enums import (
    EvidenceKind,
    IntegrityMethod,
    RetentionClass,
    Sensitivity,
    VerificationResult,
)
from nyx.domain.ids import new_evidence_id


class EvidenceVerification(NyxModel):
    """The result of checking an evidence item's integrity or provenance."""

    result: VerificationResult
    method: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    verified_at: UtcDatetime | None = None
    verified_by: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    notes: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None


class EvidenceItem(NyxModel):
    """An evidence record or reference.

    Both inline and reference-only evidence are supported. Large or restricted payloads should
    be referenced rather than copied into findings.
    """

    id: Identifier = Field(default_factory=new_evidence_id)
    schema_version: SchemaVersion = "1.0"
    kind: EvidenceKind
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    source_system_id: Identifier | None = None
    source_name: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    source_ref: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    locator: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    scope: Scope | None = None
    captured_at: UtcDatetime
    valid_from: UtcDatetime
    valid_until: UtcDatetime | None = None
    content_hash: Digest | None = None
    integrity_method: IntegrityMethod
    producer_identity: PrincipalRef | None = None
    verification: EvidenceVerification | None = None
    relevance_to_claim: Identifier | None = None
    relevance_to_finding: Identifier | None = None
    relevance_note: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None
    limitations: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(
        min_length=1, max_length=32
    )
    sensitivity: Sensitivity
    access_policy_ref: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    retention_class: RetentionClass

    @model_validator(mode="after")
    def _require_source_and_relevance(self) -> Self:
        if self.source_system_id is None and self.source_name is None:
            raise ValueError("evidence must name a source_system_id or a source_name")
        if self.relevance_to_claim is None and self.relevance_to_finding is None:
            raise ValueError("evidence must declare relevance_to_claim or relevance_to_finding")
        return self
