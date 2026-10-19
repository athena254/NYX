"""HTTP request/response models for the Phase 1 API subset (SPEC §9.1)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nyx.domain.systems import MonitoredSystem
from nyx.ingestion.errors import StructuredError


class SystemPage(BaseModel):
    """A cursor page of monitored systems."""

    model_config = ConfigDict(extra="forbid")

    items: list[MonitoredSystem]
    next_cursor: str | None = None


class EventBatchRequest(BaseModel):
    """A bounded batch of raw event payloads validated item by item."""

    model_config = ConfigDict(extra="forbid")

    events: list[dict[str, Any]] = Field(min_length=0)


class BatchResultItem(BaseModel):
    """One per-item batch outcome (SPEC §9.1, api-error-conventions §4)."""

    model_config = ConfigDict(extra="forbid")

    index: int
    status: str
    record_id: str | None = None
    reason: str | None = None
    error: StructuredError | None = None


class BatchResponse(BaseModel):
    """The per-item outcome list for a batch ingestion."""

    model_config = ConfigDict(extra="forbid")

    results: list[BatchResultItem]
    request_id: str


class HealthResponse(BaseModel):
    """Liveness response."""

    model_config = ConfigDict(extra="forbid")

    status: str
    service: str
    version: str


class ReadyResponse(BaseModel):
    """Readiness response."""

    model_config = ConfigDict(extra="forbid")

    status: str
    database: str
    migrations: str
