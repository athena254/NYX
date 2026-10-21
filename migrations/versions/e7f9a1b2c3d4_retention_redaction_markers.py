"""retention: redaction markers on events, claims, evidence items

Revision ID: e7f9a1b2c3d4
Revises: c4d5e8f0a2b3
Create Date: 2026-10-21 00:00:00.000000

Adds ``redacted_at``, ``redacted_by``, ``redaction_reason``, and ``redaction_action`` to the
three record classes that carry payload content and may therefore be tombstoned: ``events``,
``claims``, ``evidence_items``.

They are deliberately **not** added to ``findings``, ``rebuttals``, ``dispositions``,
``verification_jobs``, or ``policy_versions``. Those tables are append-only (ADR 0003) and are
never rewritten; a disposition that relied on now-redacted evidence records that the evidence was
redacted and remains contestable, rather than being mutated into agreement with the new state
(``docs/privacy-and-retention.md`` §5).

All four columns are nullable: ``redacted_at`` is null until the record is redacted, and the
other three are null for the same reason. A record with no marker is an unredacted record, which
is the common case and must not require a sentinel value.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f9a1b2c3d4"
down_revision: str | None = "c4d5e8f0a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REDACTABLE_TABLES = ("events", "claims", "evidence_items")

_MARKER_COLUMNS = (
    sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("redacted_by", sa.String(length=128), nullable=True),
    sa.Column("redaction_reason", sa.String(length=2000), nullable=True),
    sa.Column("redaction_action", sa.String(length=32), nullable=True),
)


def upgrade() -> None:
    for table in _REDACTABLE_TABLES:
        for column in _MARKER_COLUMNS:
            op.add_column(table, column.copy())


def downgrade() -> None:
    for table in reversed(_REDACTABLE_TABLES):
        op.drop_column(table, "redaction_action")
        op.drop_column(table, "redaction_reason")
        op.drop_column(table, "redacted_by")
        op.drop_column(table, "redacted_at")
