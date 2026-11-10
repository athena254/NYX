"""Separate producer provenance claims from trusted submitter metadata.

Revision ID: d6e7f8a9b0c1
Revises: c5e6f7a8b9d0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: str | None = "c5e6f7a8b9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evidence_items", sa.Column("claimed_producer_identity", sa.JSON(), nullable=True)
    )
    op.add_column("evidence_items", sa.Column("claimed_verification", sa.JSON(), nullable=True))
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE evidence_items SET claimed_producer_identity = producer_identity, "
            "claimed_verification = verification, producer_identity = NULL, verification = NULL"
        )
    )


def downgrade() -> None:
    # Do not lose claims or restore them as trusted authority. Empty/JSON-null fields
    # can be removed; populated claims require an explicit export before rollback.
    evidence = sa.table(
        "evidence_items",
        sa.column("claimed_producer_identity", sa.JSON()),
        sa.column("claimed_verification", sa.JSON()),
    )
    populated = sa.or_(
        *(sa.and_(column.is_not(None), column != sa.JSON.NULL) for column in evidence.c)
    )
    if (
        op.get_bind()
        .execute(sa.select(sa.literal(1)).select_from(evidence).where(populated).limit(1))
        .first()
    ):
        raise RuntimeError(
            "Cannot downgrade with preserved producer claims; export them before rollback."
        )
    op.drop_column("evidence_items", "claimed_verification")
    op.drop_column("evidence_items", "claimed_producer_identity")
