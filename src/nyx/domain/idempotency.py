"""The idempotency/deduplication record value object (SPEC §11.2).

Deduplication is on *identity* — a producer-supplied event id or an idempotency key — never on
content similarity. Two distinct incidents whose text merely looks alike must not be merged.
This record stores the dedupe rationale and the stable link to the original stored record.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from nyx.domain.base import Digest, Identifier, NyxModel, SchemaVersion, UtcDatetime, utc_now

DedupeScope = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=32,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]


class IdempotencyRecord(NyxModel):
    """A reserved dedupe key and the record it produced."""

    schema_version: SchemaVersion = "1.0"
    scope: DedupeScope
    key: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    record_id: Identifier
    request_hash: Digest
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    created_at: UtcDatetime = Field(default_factory=utc_now)
