"""Opaque cursor pagination with deterministic ordering (SPEC §9.2).

Cursors encode the ``(created_at, id)`` of the last item returned. IDs are time-sortable, so
ordering by that tuple is stable across inserts; a new record cannot shift an existing page.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class InvalidCursorError(ValueError):
    """Raised when a cursor cannot be decoded."""


def encode_cursor(created_at: datetime, record_id: str) -> str:
    """Encode a ``(created_at, id)`` pair as an opaque URL-safe cursor."""

    payload = {"t": created_at.astimezone(UTC).isoformat(), "id": record_id}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a cursor into an aware UTC datetime and an id."""

    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw.decode("utf-8"))
        created_at = datetime.fromisoformat(str(payload["t"]))
        record_id = str(payload["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("cursor is not valid") from exc
    if created_at.tzinfo is None:
        raise InvalidCursorError("cursor timestamp is not timezone-aware")
    if not record_id:
        raise InvalidCursorError("cursor is missing a record id")
    return created_at.astimezone(UTC), record_id


def clamp_limit(limit: int) -> int:
    """Clamp a requested page size into the supported range."""

    if limit < 1:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)
