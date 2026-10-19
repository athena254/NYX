"""Payload and batch size enforcement (SPEC §9.2).

Limits are enforced on the *inbound* bytes before parsing or redaction so that an oversized
body cannot consume unbounded memory. A payload measuring exactly the configured limit is
accepted; only a payload strictly greater than the limit is rejected.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from nyx.config import Settings
from nyx.domain.canonical import canonical_json


class LimitExceededError(Exception):
    """Raised when an inbound payload or batch exceeds a configured limit."""

    def __init__(self, *, limit: int, observed: int, kind: str) -> None:
        self.limit = limit
        self.observed = observed
        self.kind = kind
        super().__init__(f"{kind} limit exceeded: observed={observed} limit={limit}")


def measure(value: Any) -> int:
    """Return the size in bytes of a payload for limit enforcement.

    ``bytes`` are measured directly. Strings are measured as UTF-8. Anything else is measured
    by its canonical JSON encoding, matching exactly what NYX would persist.
    """

    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, BaseModel):
        return len(canonical_json(value))
    return len(canonical_json(value))


def enforce_payload_size(value: Any, settings: Settings) -> int:
    """Return the measured size, raising :class:`LimitExceededError` when it is too large."""

    size = measure(value)
    if size > settings.max_payload_bytes:
        raise LimitExceededError(
            limit=settings.max_payload_bytes, observed=size, kind="PAYLOAD_TOO_LARGE"
        )
    return size


def enforce_batch_size(count: int, settings: Settings) -> None:
    """Raise :class:`LimitExceededError` when ``count`` exceeds ``max_batch_size``."""

    if count > settings.max_batch_size:
        raise LimitExceededError(
            limit=settings.max_batch_size, observed=count, kind="BATCH_TOO_LARGE"
        )
