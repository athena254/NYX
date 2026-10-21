"""The claim aggregate (SPEC §5.3).

A claim is a proposition that can be assessed. Claims must be scoped: "all tests pass" is
not a usable claim without the artifact, commit, environment, period, or conditions it refers
to. A bare system identity is not enough to make a statement falsifiable.
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from nyx.domain.base import Identifier, NyxModel, SchemaVersion, Scope, UtcDatetime, utc_now
from nyx.domain.enums import ClaimStatus, ClaimType, RetentionClass
from nyx.domain.ids import new_claim_id


class Claim(NyxModel):
    """A scoped proposition attributed to a monitored system or principal."""

    id: Identifier = Field(default_factory=new_claim_id)
    schema_version: SchemaVersion = "1.0"
    statement: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    claimant_system_id: Identifier
    claimant_principal_id: Identifier | None = None
    claim_type: ClaimType
    scope: Scope
    created_at: UtcDatetime = Field(default_factory=utc_now)
    source_event_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    status: ClaimStatus = ClaimStatus.UNASSESSED
    # Carries the retention class the producer declared. Without it a claim cannot be classified
    # for purge, so the retention policy silently skips every claim.
    retention_class: RetentionClass = RetentionClass.STANDARD

    @model_validator(mode="after")
    def _require_scoped_claim(self) -> Self:
        if not self.scope.has_detail():
            raise ValueError(
                "claim scope must identify at least one of artifact, commit, environment, "
                "period, or conditions; a system id alone is not a scoped claim"
            )
        return self
