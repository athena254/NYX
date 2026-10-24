"""Validated configuration for the agent-transcript adapters (ADR 0010).

This model is deliberately **secret-free**. The only field is a path to the agent's own
conversation-log database. A path cannot carry a credential, and a configuration object that
could hold one would be an object that could leak one -- so there is no token field, and the
adapter never reads one.

``from_settings`` maps the ``agent_transcript_*`` fields that ``Settings`` carries (ADR 0010),
so the adapter is configured by the same environment as the rest of NYX and by nothing else.
When no path is set, the calling adapter supplies its own auto-detected default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from nyx.config import Settings


class AgentTranscriptConfig(BaseModel):
    """Validated adapter configuration: one read-only transcript database and its system id.

    ``db_path`` is the agent's own conversation-log store. It is opened **read-only** by every
    adapter, so a transcript adapter can never write to -- let alone corrupt -- the agent's live
    database. ``system_id`` is the ``source_system_id`` stamped on every emitted input.
    """

    model_config = ConfigDict(extra="forbid")

    db_path: Path
    system_id: Annotated[str, Field(min_length=1, max_length=128)]

    @classmethod
    def from_settings(cls, settings: Settings, *, default_db: Path) -> AgentTranscriptConfig:
        """Build the config from settings, auto-detecting the store when no path is set."""

        raw = settings.agent_transcript_db
        db = Path(raw) if raw else default_db
        return cls(db_path=db, system_id=settings.agent_transcript_system_id)


__all__ = ["AgentTranscriptConfig"]
