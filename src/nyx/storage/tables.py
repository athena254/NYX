"""SQLAlchemy declarative tables mapped 1:1 onto the Phase 1a domain aggregates.

Enumerations are stored as their string values. Structured fields (system metadata, event
payloads, claim scopes, lineage lists) use JSON columns. Columns that the API will filter on
are indexed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all NYX tables."""


class SystemRow(Base):
    __tablename__ = "monitored_systems"
    __table_args__ = (
        UniqueConstraint("name", "environment", name="uq_monitored_systems_name_environment"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    adapter: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    source_system_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    parent_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    subject: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_ref: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    trust_class: Mapped[str] = mapped_column(String(32), nullable=False)
    labels: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    retention_class: Mapped[str] = mapped_column(String(32), nullable=False)


class ClaimRow(Base):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    statement: Mapped[str] = mapped_column(String(2000), nullable=False)
    claimant_system_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    claimant_principal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    source_event_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class EvidenceRow(Base):
    __tablename__ = "evidence_items"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    source_system_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_ref: Mapped[str] = mapped_column(String(2048), nullable=False)
    locator: Mapped[str | None] = mapped_column(String(512), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    integrity_method: Mapped[str] = mapped_column(String(32), nullable=False)
    producer_identity: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    relevance_to_claim: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    relevance_to_finding: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    relevance_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    limitations: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    access_policy_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    retention_class: Mapped[str] = mapped_column(String(32), nullable=False)


class AuditRow(Base):
    __tablename__ = "audit_entries"
    __table_args__ = (UniqueConstraint("sequence", name="uq_audit_entries_sequence"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    prior_entry_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(80), nullable=False)
