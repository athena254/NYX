"""The redaction workflow (``docs/privacy-and-retention.md`` §5, SPEC §11.3).

The workflow has eight steps; this module owns the ones one operation can complete atomically:
request, authorise, locate, apply, preserve the fact, verify. Invalidation (step 6) is a no-op
in the MVP because nothing is cached or indexed yet -- there is no derived copy to invalidate --
and the module says so rather than implying coverage it does not have.

Two rules the module refuses to bend on:

* **``legal_hold`` is denied.** The denial explains why: a hold lasts until an authorised
  principal *releases* it, so no configured duration reaches it. The denial is itself audited.
* **Findings, rebuttals, dispositions, verification jobs, and policy versions are never touched.**
  They are append-only (ADR 0003). A disposition that relied on now-redacted evidence is not
  rewritten; the evidence's own marker, plus this audit entry, are what make that disposition
  contestable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import update
from sqlalchemy.orm import Session

from nyx.audit.writer import AuditEntryInput, AuditWriter
from nyx.domain.base import PrincipalRef, utc_now
from nyx.domain.enums import AuditOutcome
from nyx.domain.ids import new_request_id
from nyx.error_codes import ErrorCode
from nyx.retention.tombstone import TOMBSTONE_PREFIX, tombstone_payload
from nyx.storage.tables import ClaimRow, EventRow, EvidenceRow

# The three record classes that carry payload content and may therefore be tombstoned. This dict is
# the whole reach of the retention write path: the append-only dispute tables are not in it, so no
# caller can name them and no update can reach them.
_ROW_TYPES: dict[str, type[Any]] = {
    "event": EventRow,
    "claim": ClaimRow,
    "evidence": EvidenceRow,
}

SUPPORTED_TARGET_TYPES: frozenset[str] = frozenset(_ROW_TYPES)

# ``hash_removed`` is a separate action because a digest can itself be sensitive
# (privacy-and-retention §1.5) and its removal is a different obligation from a payload's.
SUPPORTED_ACTIONS: frozenset[str] = frozenset({"payload_redacted", "hash_removed"})

# Retention actions must match ``ActionName``'s dotted pattern.
_AUDIT_ACTIONS: dict[str, str] = {
    "payload_redacted": "retention.payload_redacted",
    "hash_removed": "retention.hash_removed",
}

# The payload column per row type. A tombstone replaces these, never the record's identity.
_PAYLOAD_FIELDS: dict[type[Any], tuple[str, ...]] = {
    EventRow: ("payload",),
    ClaimRow: ("statement",),
    EvidenceRow: ("description",),
}

# The digest column per row type, when there is one. Removing it is what ``hash_removed`` means.
_HASH_FIELDS: dict[type[Any], tuple[str, ...]] = {
    EventRow: ("content_hash",),
    ClaimRow: (),
    EvidenceRow: ("content_hash",),
}

# Who did it and why, written on the row so a reader need not join the audit log to learn that
# the content is gone.
_MARKER_FIELDS = ("redacted_at", "redacted_by", "redaction_reason", "redaction_action")

_PLACEHOLDER_MAX = 2000
_LEGAL_HOLD = "legal_hold"


class RetentionAuthoriser(Protocol):
    """The capability seam for the retention workflow.

    ``privacy-and-retention.md`` §5 requires the requester to hold "the retention capability".
    The lifecycle :class:`~nyx.findings.lifecycle.Capability` enum predates retention, so this
    module asks the question it needs and the orchestrator supplies the answer -- a
    :class:`~nyx.auth.checker.ConfigBackedCapabilityChecker`, or the deny-all default.
    """

    def may_redact(self, actor: PrincipalRef, target_type: str, action: str) -> bool: ...


class RedactionDeniedError(RuntimeError):
    """The redaction was refused. ``reason`` is safe to show the requester."""

    def __init__(self, reason: str, *, code: ErrorCode = ErrorCode.FORBIDDEN) -> None:
        self.reason = reason
        self.code = code
        super().__init__(reason)


class UnknownTargetError(RedactionDeniedError):
    """The target type or id does not name a redactable record."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, code=ErrorCode.NOT_FOUND)


@dataclass(frozen=True)
class RedactionOutcome:
    """The result of a completed redaction, returned to the requester (step 8)."""

    record_id: str
    action: str  # "payload_redacted" | "hash_removed"
    fields_affected: tuple[str, ...]
    audit_entry_id: str


class RedactionService:
    """Authorise, locate, tombstone, audit, and verify a single redaction."""

    def __init__(
        self, session: Session, *, actor: PrincipalRef, allowlist: RetentionAuthoriser
    ) -> None:
        self._session = session
        self._actor = actor
        self._allowlist = allowlist

    def redact(
        self, *, target_type: str, target_id: str, action: str, reason: str
    ) -> RedactionOutcome:
        """Run the workflow for one record and return the outcome.

        The row change, the marker columns, and the audit entry all land in the caller's
        transaction; nothing commits here.
        """

        self._authorise(target_type, action)
        row = self._locate(target_type, target_id)

        if str(getattr(row, "retention_class", "")) == _LEGAL_HOLD:
            self._audit_denied(target_type, target_id, reason)
            raise RedactionDeniedError(
                f"{target_type} '{target_id}' is under legal_hold and cannot be redacted; "
                "release the hold through the authorised hold-release action before requesting "
                "redaction"
            )

        tombstone = tombstone_payload(
            current_payload(row), action=action, actor=self._actor.id, reason=reason
        )[:_PLACEHOLDER_MAX]
        fields = apply_tombstone(
            self._session,
            target_type=target_type,
            record_id=row.id,
            action=action,
            reason=reason,
            actor_id=self._actor.id,
            at=utc_now(),
            tombstone=tombstone,
        )
        entry_id = self._audit(target_type, target_id, action, reason, fields)

        # Step 7: re-read the primary row and confirm the content is absent. A redaction that
        # cannot be verified is a failure, not a success with an asterisk.
        self._session.flush()
        reloaded = self._session.get(_ROW_TYPES[target_type], target_id)
        if reloaded is None:
            raise RedactionDeniedError(
                "verification failed: the record could not be re-read after redaction",
                code=ErrorCode.REDACTION_FAILED,
            )
        self._verify(reloaded, fields)

        return RedactionOutcome(
            record_id=target_id,
            action=action,
            fields_affected=fields,
            audit_entry_id=entry_id,
        )

    # -- step 2: authorise ------------------------------------------------------

    def _authorise(self, target_type: str, action: str) -> None:
        """Refuse anything the retention capability does not cover, or the action does not allow."""

        if not self._allowlist.may_redact(self._actor, target_type, action):
            raise RedactionDeniedError(
                "redaction is not permitted: the actor does not hold the retention capability"
            )
        if target_type not in SUPPORTED_TARGET_TYPES:
            raise UnknownTargetError(
                f"target_type '{target_type}' is not redactable; expected one of "
                f"{', '.join(sorted(SUPPORTED_TARGET_TYPES))}"
            )
        if action not in SUPPORTED_ACTIONS:
            raise RedactionDeniedError(
                f"action '{action}' is not supported; expected one of "
                f"{', '.join(sorted(SUPPORTED_ACTIONS))}"
            )

    # -- step 3: locate ---------------------------------------------------------

    def _locate(self, target_type: str, target_id: str) -> Any:
        """Return the primary row for the target, or refuse when it does not exist."""

        row = self._session.get(_ROW_TYPES[target_type], target_id)
        if row is None:
            raise UnknownTargetError(
                f"{target_type} '{target_id}' does not exist; nothing was changed"
            )
        return row

    # -- step 7: verify ---------------------------------------------------------

    def _verify(self, row: Any, fields: tuple[str, ...]) -> None:
        """Confirm the removed content is absent from the re-read row.

        The marker fields are excluded: they hold *who* and *why*, which is the point of the
        redaction, not content that should have been removed.
        """

        for field in fields:
            if field in _MARKER_FIELDS:
                continue
            value = getattr(row, field)
            if value is None:
                continue
            if TOMBSTONE_PREFIX not in str(value):
                raise RedactionDeniedError(
                    f"verification failed: field '{field}' still holds content after redaction",
                    code=ErrorCode.REDACTION_FAILED,
                )

    # -- step 5: preserve the fact ---------------------------------------------

    def _audit(
        self,
        target_type: str,
        target_id: str,
        action: str,
        reason: str,
        fields: tuple[str, ...],
    ) -> str:
        entry = AuditWriter(self._session).append(
            AuditEntryInput(
                actor=self._actor,
                action=_AUDIT_ACTIONS[action],
                target_type=target_type,
                target_id=target_id,
                request_id=new_request_id(),
                outcome=AuditOutcome.SUCCESS,
                reason=(
                    f"redaction applied: action={action}; fields={','.join(fields)}; "
                    f"reason={reason}; payload replaced with a tombstone"
                ),
            )
        )
        return entry.id

    def _audit_denied(self, target_type: str, target_id: str, reason: str) -> None:
        """Record a refused attempt. The fact of the *attempt* is auditable too."""

        AuditWriter(self._session).append(
            AuditEntryInput(
                actor=self._actor,
                action="retention.redaction_denied",
                target_type=target_type,
                target_id=target_id,
                request_id=new_request_id(),
                outcome=AuditOutcome.DENIED,
                reason=f"redaction denied: target is under legal_hold; requested reason={reason}",
            )
        )


def current_payload(row: Any) -> str:
    """Return the payload text a tombstone replaces (never stored, never echoed)."""

    field = _PAYLOAD_FIELDS[type(row)][0]
    value = getattr(row, field)
    return value if isinstance(value, str) else repr(value)


def tombstone_values(
    *,
    row_type: type[Any],
    action: str,
    reason: str,
    actor_id: str,
    at: datetime,
    tombstone: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Build the ``UPDATE`` values for a tombstone and the fields they cover.

    Shared by the redaction workflow and the purge pass so the two cannot drift: a purge that
    wrote a different marker shape than a redaction would leave the audit trail and the row
    disagreeing about what happened.
    """

    values: dict[str, Any] = dict.fromkeys(_PAYLOAD_FIELDS[row_type], tombstone)
    fields = list(_PAYLOAD_FIELDS[row_type])
    if action == "hash_removed":
        for field in _HASH_FIELDS[row_type]:
            values[field] = None
            fields.append(field)
    # Record who did it and why on the row itself, so a reader does not have to join the audit
    # log to learn that the content is gone, or who removed it.
    values.update(
        {
            "redacted_at": at,
            "redacted_by": actor_id,
            "redaction_reason": reason,
            "redaction_action": action,
        }
    )
    return values, (*fields, *_MARKER_FIELDS)


def apply_tombstone(
    session: Session,
    *,
    target_type: str,
    record_id: str,
    action: str,
    reason: str,
    actor_id: str,
    at: datetime,
    tombstone: str,
) -> tuple[str, ...]:
    """Write the tombstone for one row and return every field it changed.

    One ``UPDATE``, on the primary key, scoped to the three redactable tables. The append-only
    dispute tables are not reachable from here by construction: ``_ROW_TYPES`` has no entry for
    them, so a caller cannot name them.
    """

    row_type = _ROW_TYPES[target_type]
    values, fields = tombstone_values(
        row_type=row_type,
        action=action,
        reason=reason,
        actor_id=actor_id,
        at=at,
        tombstone=tombstone,
    )
    session.execute(update(row_type).where(row_type.id == record_id).values(**values))
    return fields


__all__ = [
    "RedactionDeniedError",
    "RedactionOutcome",
    "RedactionService",
    "RetentionAuthoriser",
    "SUPPORTED_ACTIONS",
    "SUPPORTED_TARGET_TYPES",
    "UnknownTargetError",
    "apply_tombstone",
    "current_payload",
    "tombstone_values",
]
