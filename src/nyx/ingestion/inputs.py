"""Producer-facing input models.

These are the shapes a producer may submit through the API, the CLI, or fixtures. They differ
from the persisted domain models in deliberate ways:

- server-assigned fields (``id``, ``received_at``, ``content_hash``) are optional and ignored
  when supplied, so replaying a previously read example file works;
- a ``producer_event_id`` may be supplied for identity-based deduplication (SPEC §11.2).

Validation is deliberately strict (``extra="forbid"``) apart from those tolerated server
fields, so a malformed payload fails closed with a structured error.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, StringConstraints

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
    AdapterKind,
    ClaimStatus,
    ClaimType,
    Environment,
    EvidenceKind,
    IntegrityMethod,
    RetentionClass,
    Sensitivity,
    SystemKind,
    SystemStatus,
    TrustClass,
)
from nyx.domain.events import EventType
from nyx.domain.evidence import EvidenceVerification
from nyx.domain.systems import DottedCapability


class SystemManifestInput(NyxModel):
    """A monitored-system manifest (SPEC §5.1)."""

    id: Identifier | None = None
    schema_version: SchemaVersion = "1.0"
    name: Annotated[
        str,
        StringConstraints(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
    ]
    kind: SystemKind
    environment: Environment
    owner: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    capabilities: list[DottedCapability] = Field(default_factory=list, max_length=64)
    adapter: AdapterKind
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=64)
    created_at: UtcDatetime | None = None
    status: SystemStatus = SystemStatus.ACTIVE


class EventInput(NyxModel):
    """A normalized observation submitted by a producer (SPEC §5.2)."""

    id: Identifier | None = None
    schema_version: SchemaVersion = "1.0"
    source_system_id: Identifier
    event_type: EventType
    occurred_at: UtcDatetime
    received_at: UtcDatetime | None = None
    correlation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    parent_event_id: Identifier | None = None
    actor: PrincipalRef | None = None
    subject: PrincipalRef | None = None
    payload: dict[str, Any] = Field(default_factory=dict, max_length=128)
    source_ref: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None = None
    content_hash: Digest | None = None
    trust_class: TrustClass
    labels: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=32
    )
    retention_class: RetentionClass
    producer_event_id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None


class ClaimInput(NyxModel):
    """A scoped proposition submitted by a producer (SPEC §5.3)."""

    id: Identifier | None = None
    schema_version: SchemaVersion = "1.0"
    statement: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    claimant_system_id: Identifier
    claimant_principal_id: Identifier | None = None
    claim_type: ClaimType
    scope: Scope
    created_at: UtcDatetime | None = None
    source_event_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    status: ClaimStatus = ClaimStatus.UNASSESSED
    producer_event_id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None


class EvidenceInput(NyxModel):
    """An evidence item or reference submitted by a producer (SPEC §5.4)."""

    id: Identifier | None = None
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
    producer_event_id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
