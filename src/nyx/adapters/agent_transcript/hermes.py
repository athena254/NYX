"""The Hermes agent-transcript adapter (ADR 0010).

Hermes (the agent this very service runs under) keeps its conversation log in a single SQLite
database at ``%LOCALAPPDATA%\\hermes\\state.db``. This adapter reads that database **read-only**
and turns each assistant turn into the producer inputs :class:`AgentTranscriptAdapter` maps from:
the turn becomes an event, its prose a claim, and each tool call plus its result an evidence
item.

The schema it reads (observed from the live database, not assumed):

* ``sessions(id, cwd, git_branch, git_repo_root, started_at, ...)`` -- one row per conversation;
* ``messages(id, session_id, role, content, tool_calls, tool_call_id, tool_name, timestamp,
  ...)`` -- one row per message. ``role`` is ``assistant`` (a turn), ``tool`` (a tool result),
  or ``session_meta``/``user`` (skipped). ``timestamp`` is epoch seconds (a float).

``tool_calls`` is a JSON array of ``{"call_id", "id", "function": {"name", "arguments"}}``; a
``tool`` row's ``tool_call_id`` links back to the assistant call, and its ``content`` is the
result. The adapter links them in memory, so a tool result lands on the same evidence item as
the call that produced it.

Nothing here is Hermes-specific beyond the SQL and the field mapping -- that is the whole point
of the template: a new agent is a new :meth:`iter_turns`, and the shared base does the rest.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nyx.adapters.agent_transcript.base import AgentTranscriptAdapter, ToolCall, Turn

#: The single role this adapter files as a turn. Everything else in the log is either a tool
#: result (linked to a turn) or metadata (ignored).
_ASSISTANT_ROLE = "assistant"
_TOOL_ROLE = "tool"

#: The SQL the three reads issue. Literals, never f-strings over a column tuple: a literal keeps
#: the query out of the S608 "string-based query construction" lint and makes a schema drift show
#: up as a clear :class:`OperationalError` on the named column rather than a silent wrong read.
_SELECT_SESSIONS = "SELECT id, cwd, git_branch, started_at FROM sessions"
_SELECT_TOOL_RESULTS = (
    "SELECT tool_call_id, content FROM messages WHERE role = ? AND tool_call_id IS NOT NULL"
)
_SELECT_TURNS = (
    "SELECT id, session_id, content, tool_calls, tool_call_id, timestamp "
    "FROM messages WHERE role = ?"
)


def default_hermes_db_path() -> Path:
    """The Hermes store's default location: ``%LOCALAPPDATA%\\hermes\\state.db``.

    Falls back to ``~/.hermes/state.db`` where there is no ``LOCALAPPDATA`` (a non-Windows host),
    so the adapter has a sensible default without a configuration value.
    """

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "hermes" / "state.db"
    return Path.home() / ".hermes" / "state.db"


def _epoch_to_utc(value: Any, fallback: datetime) -> datetime:
    """Convert a stored epoch-seconds value to an aware UTC datetime, or use ``fallback``."""

    if value is None:
        return fallback
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError):
        return fallback


def _parse_arguments(raw: object) -> dict[str, object]:
    """Parse a tool call's ``function.arguments`` -- a JSON string, a dict, or absent."""

    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {"raw": raw}
        return parsed if isinstance(parsed, dict) else {"raw": raw}
    return {"raw": str(raw)}


class HermesAdapter(AgentTranscriptAdapter):
    """Observes a Hermes ``state.db`` and emits its assistant turns as NYX producer inputs."""

    adapter_id = "hermes"
    version = "1.0.0"
    description = (
        "Observes a Hermes agent's conversation log (%LOCALAPPDATA%\\hermes\\state.db) and emits "
        "its assistant turns as NYX events, its statements as claims, and its tool results as "
        "evidence. Read-only: it opens the store read-only and never writes to the agent's "
        "database."
    )

    @classmethod
    def default_db_path(cls) -> Path:
        return default_hermes_db_path()

    def iter_turns(self) -> Iterator[Turn]:
        """Read ``state.db`` read-only and yield one :class:`Turn` per assistant message.

        Reads the whole log in three passes -- sessions, tool results, assistant turns -- and
        closes the connection before yielding, so the read-only handle never lingers on the
        agent's live database.
        """

        uri = f"file:{self._config.db_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            sessions = self._read_sessions(connection)
            tool_results = self._read_tool_results(connection)
            turns = self._read_turns(connection, sessions, tool_results)
        finally:
            connection.close()
        yield from turns

    @staticmethod
    def _read_sessions(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for id_, cwd, git_branch, started_at in connection.execute(_SELECT_SESSIONS):
            rows[str(id_)] = {
                "cwd": cwd,
                "branch": git_branch,
                "started_at": _epoch_to_utc(started_at, datetime.now(UTC)),
            }
        return rows

    @staticmethod
    def _read_tool_results(connection: sqlite3.Connection) -> dict[str, str]:
        results: dict[str, str] = {}
        for tool_call_id, content in connection.execute(_SELECT_TOOL_RESULTS, (_TOOL_ROLE,)):
            if tool_call_id is not None:
                results[str(tool_call_id)] = content or ""
        return results

    def _read_turns(
        self,
        connection: sqlite3.Connection,
        sessions: dict[str, dict[str, Any]],
        tool_results: dict[str, str],
    ) -> list[Turn]:
        turns: list[Turn] = []
        for row in connection.execute(_SELECT_TURNS, (_ASSISTANT_ROLE,)):
            id_, session_id, content, tool_calls_raw, _tool_call_id, timestamp = row
            session = sessions.get(str(session_id), {})
            occurred_at = _epoch_to_utc(timestamp, session.get("started_at", datetime.now(UTC)))
            calls = self._parse_tool_calls(tool_calls_raw, tool_results)
            turns.append(
                Turn(
                    turn_id=str(id_),
                    session_id=str(session_id),
                    occurred_at=occurred_at,
                    statement=(content or ""),
                    workspace=_as_text(session.get("cwd")),
                    branch=_as_text(session.get("branch")),
                    calls=tuple(calls),
                )
            )
        return turns

    @staticmethod
    def _parse_tool_calls(tool_calls_raw: object, tool_results: dict[str, str]) -> list[ToolCall]:
        if not tool_calls_raw:
            return []
        try:
            if isinstance(tool_calls_raw, str):
                calls = json.loads(tool_calls_raw)
            else:
                calls = tool_calls_raw
        except (ValueError, TypeError):
            return []
        if not isinstance(calls, list):
            return []
        parsed: list[ToolCall] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            name = function.get("name") if isinstance(function, dict) else None
            call_id = call.get("call_id") or call.get("id") or str(len(parsed))
            if not name:
                continue
            result = tool_results.get(str(call.get("call_id")))
            if result is None and call.get("id"):
                result = tool_results.get(str(call["id"]))
            arguments_source = function.get("arguments") if isinstance(function, dict) else None
            parsed.append(
                ToolCall(
                    call_id=str(call_id),
                    tool=str(name),
                    arguments=_parse_arguments(arguments_source),
                    result=result or "",
                )
            )
        return parsed


def _as_text(value: object) -> str | None:
    return str(value) if value else None


__all__ = ["HermesAdapter", "default_hermes_db_path"]
