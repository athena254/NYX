"""llm critic runs: provider, prompt version, and optional prompt/response storage

Revision ID: f8a2b3c4d5e6
Revises: b1c2d3e4f5a6
Create Date: 2026-10-24 00:00:00.000000

SPEC §13 requires a model critic to persist which provider and model it used and which
prompt-template version it ran. ``critic_runs`` already had ``model_version``; this migration
adds the provider and prompt-template version beside it.

``llm_io`` is nullable and is written only when prompt logging is explicitly enabled. When it is
written the payload has already passed through the ingestion redaction pipeline; the column is
not a place to stash raw prompts by default.

All columns are nullable. A deterministic critic has no provider, prompt template, or model I/O,
and must not need a sentinel value to say so.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8a2b3c4d5e6"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("critic_runs", sa.Column("model_provider", sa.String(length=64), nullable=True))
    op.add_column("critic_runs", sa.Column("prompt_version", sa.String(length=64), nullable=True))
    op.add_column("critic_runs", sa.Column("llm_io", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("critic_runs", "llm_io")
    op.drop_column("critic_runs", "prompt_version")
    op.drop_column("critic_runs", "model_provider")
