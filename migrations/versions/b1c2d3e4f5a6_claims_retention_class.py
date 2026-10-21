"""claims: carry the retention class the producer declared

Revision ID: b1c2d3e4f5a6
Revises: e7f9a1b2c3d4
Create Date: 2026-10-21 00:00:00.000000

Adds ``retention_class`` to ``claims``.

Why this is a bug fix and not a feature: ``ClaimInput`` has accepted a ``retention_class`` since
Phase 1b, but neither the ``Claim`` domain model nor the ``claims`` table had the column, so the
value a producer sent was validated and then silently discarded. A claim therefore could not be
classified for retention, and ``RetentionService.purge_due`` -- which reads ``retention_class``
off every row it scans -- skipped every claim forever while reporting success.

``events`` and ``evidence_items`` already carried the column from the initial schema. ``claims``
is the one that did not.

Existing rows get ``'standard'``, the operational default in ``docs/privacy-and-retention.md`` §4.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "e7f9a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The server default exists only to fill rows that already exist, and it is KEPT: SQLite has no
    # ALTER COLUMN ... DROP DEFAULT, so removing it would make this migration fail on the local
    # development database. It is harmless -- the ORM always supplies the column on insert, so the
    # default is never what writes a value.
    op.add_column(
        "claims",
        sa.Column(
            "retention_class",
            sa.String(length=32),
            nullable=False,
            server_default="standard",
        ),
    )


def downgrade() -> None:
    op.drop_column("claims", "retention_class")
