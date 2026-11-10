"""Regression coverage for the v0.0.2 assessment migration topology.

The assessment revision is a new, unshipped leaf.  It must extend the current repository
head directly: an older ancestor would leave two Alembic heads and prevent a fresh database
from upgrading.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ASSESSMENT_REVISION = "v002_assessment"
CURRENT_BASE_HEAD = "d6e7f8a9b0c1"


def test_assessment_migration_is_the_only_head_and_extends_current_base_head() -> None:
    """The candidate must have one head and directly descend from ground-truth columns."""

    config = Config((REPOSITORY_ROOT / "alembic.ini").as_posix())
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == [ASSESSMENT_REVISION]
    revision = script.get_revision(ASSESSMENT_REVISION)
    assert revision is not None
    assert revision.down_revision == CURRENT_BASE_HEAD
