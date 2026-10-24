"""Agent-transcript adapters (ADR 0010).

A family of producer adapters that observe an agent's own conversation log -- Hermes today,
OpenCode next -- and emit its turns as NYX events, its statements as claims, and its tool
results as evidence. They share one base class, :class:`AgentTranscriptAdapter`, so a new agent
is a new :meth:`~AgentTranscriptAdapter.iter_turns`, nothing more.

Like every module under ``nyx.adapters``, this package imports up into ``nyx.domain`` and
``nyx.ingestion`` and never across into the deterministic core. Only ``nyx.api.app`` and
``nyx.cli.main`` construct these adapters (gate G7, ADR 0009).
"""

from __future__ import annotations

from nyx.adapters.agent_transcript.base import (
    MIN_STATEMENT_LENGTH,
    AgentTranscriptAdapter,
    ToolCall,
    Turn,
)
from nyx.adapters.agent_transcript.config import AgentTranscriptConfig
from nyx.adapters.agent_transcript.errors import NormalizationError, TranscriptError
from nyx.adapters.agent_transcript.hermes import HermesAdapter, default_hermes_db_path

__all__ = [
    "AgentTranscriptAdapter",
    "AgentTranscriptConfig",
    "HermesAdapter",
    "MIN_STATEMENT_LENGTH",
    "NormalizationError",
    "ToolCall",
    "TranscriptError",
    "Turn",
    "default_hermes_db_path",
]
