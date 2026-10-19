"""Typed ingestion errors and per-item results.

``StructuredError`` is the single error envelope shared by the HTTP API, the batch
per-item results, and the CLI in ``--json`` mode (``docs/api-error-conventions.md``). It is
defined here, in the ingestion layer, because the ingestion pipeline is the first component
that must return structured, value-free failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from nyx.error_codes import ErrorCode, default_message_for


class FieldDetail(BaseModel):
    """A field-level validation failure. Never contains the offending value."""

    model_config = ConfigDict(extra="forbid")

    field: str
    issue: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    code: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None


class StructuredError(BaseModel):
    """The NYX error envelope."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    request_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    field_details: list[FieldDetail] = Field(default_factory=list)


IngestStatus = Literal["created", "duplicate", "rejected"]


@dataclass(frozen=True)
class IngestResult:
    """The outcome of a single ingestion attempt."""

    status: IngestStatus
    record_id: str | None = None
    error: StructuredError | None = None
    record: BaseModel | None = None

    @property
    def accepted(self) -> bool:
        """Return whether the item was created or recognised as a duplicate."""

        return self.status in {"created", "duplicate"}


def error_for(
    code: ErrorCode,
    *,
    request_id: str,
    message: str | None = None,
    field_details: list[FieldDetail] | None = None,
) -> StructuredError:
    """Build a :class:`StructuredError` from the catalogue with an optional override message."""

    return StructuredError(
        code=code.value,
        message=message or default_message_for(code),
        request_id=request_id,
        field_details=field_details or [],
    )


def rejected(
    code: ErrorCode,
    *,
    request_id: str,
    message: str | None = None,
    field_details: list[FieldDetail] | None = None,
) -> IngestResult:
    """Return a rejected :class:`IngestResult` for ``code``."""

    return IngestResult(
        status="rejected",
        error=error_for(code, request_id=request_id, message=message, field_details=field_details),
    )
