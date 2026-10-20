"""disputes: rebuttals, verification jobs, dispositions, policy versions

Revision ID: c4d5e8f0a2b3
Revises: a13408f58053
Create Date: 2026-10-20 00:00:00.000000

All four tables are append-only. This migration creates them; no later migration may add an update
or delete path. A disposition supersedes its predecessor by storing its id in
``supersedes_disposition_id`` -- the predecessor row is never rewritten.

The ``uq_dispositions_superseded_once`` constraint keeps the chain from branching: one disposition
may be superseded at most once. NULLs compare as distinct in both SQLite and PostgreSQL, so the
constraint does not limit how many dispositions may be first in a chain for one finding.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e8f0a2b3"
down_revision: str | None = "a13408f58053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rebuttals",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("finding_id", sa.String(length=128), nullable=False),
        sa.Column("submitted_by", sa.String(length=128), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("argument", sa.String(length=4000), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("requested_outcome", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rebuttals_finding_id"), "rebuttals", ["finding_id"], unique=False)
    op.create_index(op.f("ix_rebuttals_submitted_at"), "rebuttals", ["submitted_at"], unique=False)
    op.create_index(
        op.f("ix_rebuttals_requested_outcome"), "rebuttals", ["requested_outcome"], unique=False
    )

    op.create_table(
        "verification_jobs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("finding_id", sa.String(length=128), nullable=False),
        sa.Column("check_id", sa.String(length=128), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("result_summary", sa.String(length=4000), nullable=True),
        sa.Column("independence", sa.String(length=2000), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("tool_run_refs", sa.JSON(), nullable=False),
        sa.Column("failure_details", sa.String(length=4000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_verification_jobs_finding_id"), "verification_jobs", ["finding_id"], unique=False
    )
    op.create_index(
        op.f("ix_verification_jobs_check_id"), "verification_jobs", ["check_id"], unique=False
    )
    op.create_index(
        op.f("ix_verification_jobs_status"), "verification_jobs", ["status"], unique=False
    )

    op.create_table(
        "dispositions",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("finding_id", sa.String(length=128), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("rationale", sa.String(length=4000), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verification_run_ids", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=256), nullable=False),
        sa.Column("supersedes_disposition_id", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "finding_id",
            "supersedes_disposition_id",
            name="uq_dispositions_superseded_once",
        ),
    )
    op.create_index(
        op.f("ix_dispositions_finding_id"), "dispositions", ["finding_id"], unique=False
    )
    op.create_index(op.f("ix_dispositions_outcome"), "dispositions", ["outcome"], unique=False)
    op.create_index(
        op.f("ix_dispositions_decided_at"), "dispositions", ["decided_at"], unique=False
    )
    op.create_index(
        op.f("ix_dispositions_supersedes_disposition_id"),
        "dispositions",
        ["supersedes_disposition_id"],
        unique=False,
    )

    op.create_table(
        "policy_versions",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("rule_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=256), nullable=False),
        sa.Column("digest", sa.String(length=97), nullable=False),
        sa.Column("changed_by", sa.String(length=128), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("change_reason", sa.String(length=4000), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "version", name="uq_policy_versions_rule_version"),
    )
    op.create_index(
        op.f("ix_policy_versions_rule_id"), "policy_versions", ["rule_id"], unique=False
    )
    op.create_index(
        op.f("ix_policy_versions_changed_by"), "policy_versions", ["changed_by"], unique=False
    )
    op.create_index(
        op.f("ix_policy_versions_changed_at"), "policy_versions", ["changed_at"], unique=False
    )


def downgrade() -> None:
    op.drop_table("policy_versions")
    op.drop_table("dispositions")
    op.drop_table("verification_jobs")
    op.drop_table("rebuttals")
