"""The audit entry hash shared by the writer and the verifier.

Keeping one implementation is deliberate: the writer and the verifier recomputing the digest with
two slightly different payloads is the kind of drift that hides the same bug on both sides. The
payload is the canonical JSON of every stored field except ``id``, ``entry_hash``, and the row's
storage representation.

Trust limits
------------
The digest detects accidental mutation, partial writes, and out-of-order edits *within the stored
sequence*. It does not defeat a privileged attacker who can rewrite the whole database and
recompute every hash (SPEC §5.8, ADR 0003).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nyx.domain.audit import AuditTarget
from nyx.domain.base import PrincipalRef, format_utc
from nyx.domain.canonical import content_hash
from nyx.domain.enums import AuditOutcome


def entry_hash(
    *,
    schema_version: str,
    sequence: int,
    actor: PrincipalRef,
    action: str,
    target: AuditTarget,
    timestamp: datetime,
    request_id: str,
    correlation_id: str | None,
    outcome: AuditOutcome,
    reason: str,
    prior_entry_hash: str | None,
) -> str:
    """Return the digest for one audit entry, including its link to the previous entry."""

    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "sequence": sequence,
        "actor": actor.model_dump(mode="json", by_alias=True),
        "action": action,
        "target": target.model_dump(mode="json", by_alias=True),
        "timestamp": format_utc(timestamp),
        "request_id": request_id,
        "correlation_id": correlation_id,
        "outcome": outcome.value,
        "reason": reason,
        "prior_entry_hash": prior_entry_hash,
    }
    return content_hash(payload)
