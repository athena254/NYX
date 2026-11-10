"""Operational assessment work state; imported explicitly by the application bootstrap."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nyx.storage.tables import Base


class AssessmentJobRow(Base):
    """Mutable at-least-once assessment work; findings and runs remain append-only."""

    __tablename__ = "assessment_jobs"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_assessment_jobs_dedupe_key"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    trigger_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(80), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    leased_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
