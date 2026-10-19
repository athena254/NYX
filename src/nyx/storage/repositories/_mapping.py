"""Small conversion helpers shared by repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel


def as_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime; SQLite may hand back naive values."""

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_json(value: BaseModel) -> dict[str, Any]:
    """Serialise a nested domain value for a JSON column or JSON field."""

    return value.model_dump(mode="json", by_alias=True)
