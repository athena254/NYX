"""idempotency records for identity-based deduplication

Revision ID: b2c4d6e8f0a1
Revises: 9ce2370d7c1a
Create Date: 2026-10-19 20:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c4d6e8f0a1"
down_revision: str | None = "9ce2370d7c1a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=256), nullable=False),
        sa.Column("record_id", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=80), nullable=False),
        sa.Column("rationale", sa.String(length=2000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope", "dedupe_key"),
    )
    op.create_index(
        op.f("ix_idempotency_records_record_id"),
        "idempotency_records",
        ["record_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_idempotency_records_created_at"),
        "idempotency_records",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_idempotency_records_created_at"), table_name="idempotency_records")
    op.drop_index(op.f("ix_idempotency_records_record_id"), table_name="idempotency_records")
    op.drop_table("idempotency_records")
