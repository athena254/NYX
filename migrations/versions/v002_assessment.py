"""Durable event/evidence assessment work.

Revision ID: v002_assessment
Revises: c5e6f7a8b9d0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "v002_assessment"
down_revision: str | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assessment_jobs",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("trigger_kind", sa.String(length=32), nullable=False),
        sa.Column("trigger_record_id", sa.String(length=128), nullable=False),
        sa.Column("dedupe_key", sa.String(length=80), nullable=False),
        sa.Column("input_digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error_type", sa.String(length=128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("dedupe_key", name="uq_assessment_jobs_dedupe_key"),
    )
    op.create_index("ix_assessment_jobs_event_id", "assessment_jobs", ["event_id"])
    op.create_index("ix_assessment_jobs_status", "assessment_jobs", ["status"])
    op.add_column(
        "critic_runs", sa.Column("assessment_job_id", sa.String(length=128), nullable=True)
    )
    op.create_index("ix_critic_runs_assessment_job_id", "critic_runs", ["assessment_job_id"])
    op.create_index(
        "uq_critic_runs_assessment_job_critic",
        "critic_runs",
        ["assessment_job_id", "critic_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_critic_runs_assessment_job_critic", table_name="critic_runs")
    op.drop_index("ix_critic_runs_assessment_job_id", table_name="critic_runs")
    op.drop_column("critic_runs", "assessment_job_id")
    op.drop_index("ix_assessment_jobs_status", table_name="assessment_jobs")
    op.drop_index("ix_assessment_jobs_event_id", table_name="assessment_jobs")
    op.drop_table("assessment_jobs")
