"""The event aggregate (SPEC §5.2).

An event is a normalized observation, not a verified fact. ``occurred_at`` is claimed by the
producer and is not trusted merely because it is present; ``received_at`` is assigned by NYX
and preserved separately. Both are timezone-aware UTC.
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
    UtcDatetime,
    utc_now,
)
from nyx.domain.canonical import content_hash
from nyx.domain.enums import RetentionClass, TrustClass
from nyx.domain.ids import new_event_id

EventType = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9]+(\.[a-z0-9][a-z0-9_-]*)+$",
    ),
]


class Event(NyxModel):
    """A normalized observation submitted by or about a monitored system."""

    id: Identifier = Field(default_factory=new_event_id)
    schema_version: SchemaVersion = "1.0"
    source_system_id: Identifier
    event_type: EventType
    occurred_at: UtcDatetime
    received_at: UtcDatetime = Field(default_factory=utc_now)
    correlation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    parent_event_id: Identifier | None = None
    actor: PrincipalRef | None = None
    subject: PrincipalRef | None = None
    payload: dict[str, Any] = Field(max_length=128)
    source_ref: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None = None
    content_hash: Digest
    trust_class: TrustClass
    labels: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=32
    )
    retention_class: RetentionClass

    def calculated_content_hash(self) -> str:
        """Return the digest of this event's canonical representation.

        The existing ``content_hash`` field is excluded so a caller can compare a supplied
        digest with the one the canonical stored representation would produce.
        """

        return content_hash(self.model_dump(mode="json", by_alias=True, exclude={"content_hash"}))
