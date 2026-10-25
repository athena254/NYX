"""findings: observation links, subject version, reproducibility, and root-cause identity

Revision ID: c5e6f7a8b9d0
Revises: e3f5a7c9b1d2
Create Date: 2026-10-25 00:00:00.000000

Six columns on ``findings``, all additive and all nullable, so no existing row is rewritten and no
existing reader breaks.

``observation_ids`` is deliberately **not** folded into ``evidence_ids``. An evidence id is
something a producer reported; an observation id is something NYX gathered itself. Collapsing the
two is exactly how a claim ends up looking verified when it is only reported, and the whole point
of the ground-truth layer is that those are different things.

``root_cause_key`` is indexed because the read it serves is "does another finding already report
this underlying defect?" -- the deduplication C-002 and C-009 need so that one conflict does not
raise two findings. ``subject_version`` and ``reproducibility`` are indexed for the same reason:
they are the two fields a reader filters on to decide whether a finding still applies.

``remediation_hint`` is a short column rather than living inside ``reasoning`` because it is
advice for the operator, not part of the audit trail.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5e6f7a8b9d0"
down_revision: str | None = "e3f5a7c9b1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite cannot add a NOT NULL column without a default, so the JSON column carries a
    # server default for existing rows. It stays: it is what makes a raw insert into findings
    # safe rather than a NULL in a column the model declares non-nullable.
    op.add_column(
        "findings",
        sa.Column("observation_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("findings", sa.Column("root_cause_key", sa.String(length=80), nullable=True))
    op.create_index(
        op.f("ix_findings_root_cause_key"), "findings", ["root_cause_key"], unique=False
    )
    op.add_column("findings", sa.Column("subject_version", sa.String(length=256), nullable=True))
    op.create_index(
        op.f("ix_findings_subject_version"), "findings", ["subject_version"], unique=False
    )
    op.add_column("findings", sa.Column("reproducibility", sa.String(length=32), nullable=True))
    op.create_index(
        op.f("ix_findings_reproducibility"), "findings", ["reproducibility"], unique=False
    )
    op.add_column(
        "findings", sa.Column("verification_strength", sa.String(length=48), nullable=True)
    )
    op.add_column("findings", sa.Column("remediation_hint", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("findings", "remediation_hint")
    op.drop_column("findings", "verification_strength")
    op.drop_index(op.f("ix_findings_reproducibility"), table_name="findings")
    op.drop_column("findings", "reproducibility")
    op.drop_index(op.f("ix_findings_subject_version"), table_name="findings")
    op.drop_column("findings", "subject_version")
    op.drop_index(op.f("ix_findings_root_cause_key"), table_name="findings")
    op.drop_column("findings", "root_cause_key")
    op.drop_column("findings", "observation_ids")
