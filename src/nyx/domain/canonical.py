"""Canonical JSON serialisation and content digests.

**A hash is an integrity identifier, not proof of truth.** ``content_hash`` returns a digest
that lets NYX detect a change in the exact bytes it recorded. It does not establish who
produced the content, that the content is complete, that it is relevant to a claim, or that
any statement inside it is true. A privileged actor who can rewrite both the content and the
recorded digest can also rewrite this value.

Canonical form
--------------
- object keys sorted lexicographically by Unicode code point;
- no insignificant whitespace (``separators=(",", ":")``);
- UTF-8 bytes;
- non-ASCII characters preserved (``ensure_ascii=False``);
- NaN and infinity rejected because they are not valid JSON.

Pydantic models are serialised with aliases so field aliases such as ``from`` round-trip to
their schema names. Datetimes use the domain's UTC ``Z`` representation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from nyx.domain.base import format_utc


def canonical_json(value: object) -> bytes:
    """Return the canonical UTF-8 JSON encoding of a JSON-compatible value."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    )
    return text.encode("utf-8")


def _json_default(value: Any) -> Any:
    """Serialise the non-JSON scalars NYX legitimately carries, so a hash cannot fail on them.

    A ``BaseModel`` nested inside a plain dict -- a ``Scope`` inside a dedupe key, say -- is not
    reached by the top-level model dump, and its datetimes arrive as ``datetime`` objects. Without
    this, any payload containing one raised ``TypeError`` *after* the caller had already built the
    object, so a hash that is supposed to be a pure function of its input failed on ordinary data.
    """

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, datetime):
        return format_utc(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def canonical_text(value: object) -> str:
    """Return canonical JSON as text; primarily useful for debugging and tests."""

    return canonical_json(value).decode("utf-8")


def hash_bytes(payload: bytes) -> str:
    """Return ``sha256:<hex>`` for raw bytes."""

    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def content_hash(payload: Any) -> str:
    """Return ``sha256:<hex>`` of the canonical JSON representation of ``payload``."""

    return hash_bytes(canonical_json(payload))
