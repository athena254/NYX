"""ground-truth layer: observations from NYX's own probes, and authorisation records

Revision ID: e3f5a7c9b1d2
Revises: f8a2b3c4d5e6
Create Date: 2026-10-25 00:00:00.000000

Two new tables, both additive. Nothing existing is altered.

``observations`` is what NYX gathered through a probe about the real end state of a subject. It
exists beside ``evidence_items`` rather than inside it: evidence is what a producer *reported*,
an observation is what NYX *saw*. Keeping them apart is what makes "the booking exists in the
database" checkable independently of "the agent said it booked". ``verification_strength`` is
indexed because the primary read is "is there an observation at least this strong for this
subject version", which is the upgraded C-001's gate.

``authorisations`` records who approved a risky action, for what scope, until when. The existing
C-006 reads an ``approval_ref`` payload field and can only check that a string was present; it
cannot express an approver that differs from the actor, a scope that does not cover the action,
or an expiry that has passed. Those three are exactly the cases where a reference gives false
comfort, so the record type is added rather than the check being weakened.

Both tables are additive and nullable-free where a value is genuinely required. No column is
dropped and no existing row is rewritten, so a deployment on ``f8a2b3c4d5e6`` upgrades in place.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3f5a7c9b1d2"
down_revision: str | None = "f8a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "observations",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("probe_id", sa.String(length=128), nullable=False),
        sa.Column("probe_version", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("subject_version", sa.String(length=256), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collector", sa.JSON(), nullable=False),
        sa.Column("method", sa.String(length=64), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("raw_artifact_hash", sa.String(length=80), nullable=True),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("verification_strength", sa.String(length=48), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=True),
        sa.Column("relevance_to_claim", sa.String(length=128), nullable=True),
        sa.Column("detail", sa.String(length=2000), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "probe_id",
        "subject",
        "subject_version",
        "collected_at",
        "result",
        "environment",
        "verification_strength",
        "relevance_to_claim",
    ):
        op.create_index(op.f(f"ix_observations_{column}"), "observations", [column], unique=False)

    op.create_table(
        "authorisations",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("risk_action", sa.String(length=48), nullable=False),
        sa.Column("actor", sa.JSON(), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("approver", sa.JSON(), nullable=False),
        sa.Column("approver_id", sa.String(length=128), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("scope_ref", sa.String(length=256), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("subject_system_id", sa.String(length=128), nullable=True),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("granted_by", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_id", "action", "approved_at", name="uq_authorisations_identity"),
    )
    for column in (
        "action",
        "approver_id",
        "environment",
        "approved_at",
        "subject_system_id",
    ):
        op.create_index(
            op.f(f"ix_authorisations_{column}"), "authorisations", [column], unique=False
        )


def downgrade() -> None:
    for column in (
        "subject_system_id",
        "approved_at",
        "environment",
        "approver_id",
        "action",
    ):
        op.drop_index(op.f(f"ix_authorisations_{column}"), table_name="authorisations")
    op.drop_table("authorisations")

    for column in (
        "relevance_to_claim",
        "verification_strength",
        "environment",
        "result",
        "collected_at",
        "subject_version",
        "subject",
        "probe_id",
    ):
        op.drop_index(op.f(f"ix_observations_{column}"), table_name="observations")
    op.drop_table("observations")
