"""The audit-entry aggregate (SPEC §5.8).

Audit entries are append-only. ``prior_entry_hash`` links an entry to the hash of the previous
entry, so an accidental or partial edit within the stored sequence is detectable. It does not
defeat a privileged attacker who can rewrite the whole database and recompute the chain.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from nyx.domain.base import (
    ActionName,
    Digest,
    Identifier,
    NyxModel,
    PrincipalRef,
    SchemaVersion,
    UtcDatetime,
    utc_now,
)
from nyx.domain.enums import AuditOutcome
from nyx.domain.ids import new_audit_id


class AuditTarget(NyxModel):
    """The record type and identifier an audit entry concerns."""

    type: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=64,
            pattern=r"^[a-z][a-z0-9_]*$",
        ),
    ]
    id: Identifier


class AuditEntry(NyxModel):
    """One immutable entry in the audit history."""

    id: Identifier = Field(default_factory=new_audit_id)
    schema_version: SchemaVersion = "1.0"
    sequence: int = Field(ge=1)
    actor: PrincipalRef
    action: ActionName
    target: AuditTarget
    timestamp: UtcDatetime = Field(default_factory=utc_now)
    request_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    correlation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    outcome: AuditOutcome
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    prior_entry_hash: Digest | None = None
    entry_hash: Digest
