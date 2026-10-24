"""Unit tests for the agent-transcript adapter base and the Hermes adapter (ADR 0010).

These tests exercise the adapter against a **seeded** transcript database the test controls, so
the mapping from a turn to producer inputs is asserted exactly, without depending on the live
``state.db`` (which is large, personal, and changes between runs). The live-DB proof lives in the
contract test; these assert the pure logic: health, the turn-to-inputs mapping, stable identities,
read-only access, and the two typed refusals.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nyx.adapters.agent_transcript.base import (
    MIN_STATEMENT_LENGTH,
    AgentTranscriptAdapter,
    ToolCall,
    Turn,
)
from nyx.adapters.agent_transcript.config import AgentTranscriptConfig
from nyx.adapters.agent_transcript.errors import NormalizationError, TranscriptError
from nyx.adapters.agent_transcript.hermes import HermesAdapter
from nyx.config import Settings
from nyx.ingestion.inputs import ClaimInput, EventInput, EvidenceInput

#: The Hermes schema the tests seed. Kept in one place so a schema change is a one-line edit.
_MESSAGE_INSERT = (
    "INSERT INTO messages (id, session_id, role, content, tool_calls, tool_call_id, timestamp) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def _seed_hermes_db(path: Path) -> None:
    """Create a minimal Hermes-shaped store and seed it with a small, known conversation."""

    connection = sqlite3.connect(path.as_posix())
    try:
        connection.execute(
            "CREATE TABLE sessions "
            "(id TEXT PRIMARY KEY, cwd TEXT, git_branch TEXT, started_at REAL)"
        )
        connection.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
            "tool_calls TEXT, tool_call_id TEXT, timestamp REAL)"
        )
        connection.execute(
            "INSERT INTO sessions (id, cwd, git_branch, started_at) VALUES (?, ?, ?, ?)",
            ("s1", "C:/proj/app", "main", 1_792_000_000.0),
        )
        messages = [
            # A substantive claim, no tools.
            (
                1,
                "s1",
                "assistant",
                "The tests all pass and the build is green.",
                None,
                None,
                1_792_000_100.0,
            ),
            # A tool-only turn: no claim, one evidence item whose result comes from a tool row.
            (
                2,
                "s1",
                "assistant",
                "",
                '[{"call_id": "c1", "id": "c1", "function": {"name": "terminal", '
                '"arguments": "{\\"command\\": \\"pytest -q\\"}"}}]',
                None,
                1_792_000_200.0,
            ),
            # A turn with both a claim and a tool call.
            (
                3,
                "s1",
                "assistant",
                "I ran the full test suite after the fix.",
                '[{"call_id": "c2", "id": "c2", "function": {"name": "terminal", '
                '"arguments": {}}}]',
                None,
                1_792_000_300.0,
            ),
            # Tool results, linked by tool_call_id.
            (4, "s1", "tool", "OK", None, "c1", 1_792_000_201.0),
            (5, "s1", "tool", "648 passed in 12s", None, "c2", 1_792_000_301.0),
            # Non-assistant roles are ignored.
            (6, "s1", "user", "please run the tests", None, None, 1_792_000_050.0),
            (7, "s1", "session_meta", "{}", None, None, 1_792_000_000.0),
            # A too-short statement: an event but no claim.
            (8, "s1", "assistant", "ok", None, None, 1_792_000_400.0),
        ]
        connection.executemany(_MESSAGE_INSERT, messages)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture()
def transcript_db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    _seed_hermes_db(path)
    return path


@pytest.fixture()
def adapter(transcript_db: Path) -> HermesAdapter:
    settings = Settings(agent_transcript_db=transcript_db.as_posix())
    return HermesAdapter(AgentTranscriptConfig.from_settings(settings, default_db=transcript_db))


# --------------------------------------------------------------------------- health


def test_health_ok_when_the_store_is_readable(adapter: HermesAdapter) -> None:
    health = adapter.health()
    assert health.state.value == "ok"
    assert health.adapter_id == "hermes"


def test_health_degraded_when_the_store_is_missing(tmp_path: Path) -> None:
    settings = Settings(agent_transcript_db=(tmp_path / "nope.db").as_posix())
    adapter = HermesAdapter(AgentTranscriptConfig.from_settings(settings, default_db=tmp_path))
    health = adapter.health()
    assert health.state.value == "degraded"


def test_fetch_for_ref_raises_when_the_store_is_missing(tmp_path: Path) -> None:
    settings = Settings(agent_transcript_db=(tmp_path / "nope.db").as_posix())
    adapter = HermesAdapter(AgentTranscriptConfig.from_settings(settings, default_db=tmp_path))
    with pytest.raises(TranscriptError):
        adapter.fetch_for_ref("main")


# --------------------------------------------------------------------------- the mapping


def test_fetch_emits_events_claims_and_evidence(adapter: HermesAdapter) -> None:
    inputs = adapter.fetch_for_ref("main")
    events = [i for i in inputs if isinstance(i, EventInput)]
    claims = [i for i in inputs if isinstance(i, ClaimInput)]
    evidence = [i for i in inputs if isinstance(i, EvidenceInput)]

    # Four assistant turns -> four events; two substantive statements -> two claims;
    # two tool calls -> two evidence items.
    assert len(events) == 4
    assert len(claims) == 2
    assert len(evidence) == 2


def test_a_tool_result_is_linked_to_its_call_result(adapter: HermesAdapter) -> None:
    inputs = adapter.fetch_for_ref("main")
    evidence = {i.source_ref: i for i in inputs if isinstance(i, EvidenceInput)}
    # The two tool calls c1 and c2 carry the results their tool rows recorded.
    c1 = next(e for e in evidence.values() if "tool/c1" in e.source_ref)
    c2 = next(e for e in evidence.values() if "tool/c2" in e.source_ref)
    assert "OK" in c1.description
    assert "648 passed" in c2.description


def test_a_turn_without_a_substantive_statement_files_no_claim(adapter: HermesAdapter) -> None:
    inputs = adapter.fetch_for_ref("main")
    events = {i.id: i for i in inputs if isinstance(i, EventInput)}
    # The tool-only turn (id 2) and the "ok" turn (id 8) have no claim.
    claim_event_ids = {c.source_event_ids[0] for c in inputs if isinstance(c, ClaimInput)}
    assert "agent:evt:2" not in claim_event_ids
    assert "agent:evt:8" not in claim_event_ids
    # But both still produced events.
    assert "agent:evt:2" in events
    assert "agent:evt:8" in events


def test_the_event_payload_links_the_claim_and_evidence(adapter: HermesAdapter) -> None:
    inputs = adapter.fetch_for_ref("main")
    # Turn 3 has both a statement and a tool call, so it links a claim and evidence.
    claim = next(i for i in inputs if isinstance(i, ClaimInput) and i.id == "agent:clm:3")
    event = next(i for i in inputs if isinstance(i, EventInput) and i.id == "agent:evt:3")
    evidence = [
        i for i in inputs if isinstance(i, EvidenceInput) and i.relevance_to_claim == claim.id
    ]

    assert event.payload["claim_id"] == claim.id
    assert set(event.payload["evidence_ids"]) == {e.id for e in evidence}
    # Evidence points back at the claim it supports, so the findings service can link them.
    for item in evidence:
        assert item.relevance_to_claim == claim.id


def test_non_turn_input_raises_a_typed_normalization_error(adapter: HermesAdapter) -> None:
    with pytest.raises(NormalizationError) as excinfo:
        adapter.normalize({"not": "a turn"})
    assert excinfo.value.field == "raw"


# --------------------------------------------------------------------------- stable identity


def test_producer_event_ids_are_stable_and_unique(adapter: HermesAdapter) -> None:
    first = adapter.fetch_for_ref("main")
    second = adapter.fetch_for_ref("main")
    assert [i.producer_event_id for i in first] == [i.producer_event_id for i in second]
    assert len({i.producer_event_id for i in first}) == len(first)


def test_ids_are_supplied_so_linkage_survives_a_reingest(adapter: HermesAdapter) -> None:
    first = adapter.fetch_for_ref("main")
    second = adapter.fetch_for_ref("main")
    assert sorted(i.id for i in first) == sorted(i.id for i in second)


def test_ref_is_accepted_but_not_applied(adapter: HermesAdapter) -> None:
    # A conversation is not scoped to a git ref, so any ref yields the same whole log.
    assert len(adapter.fetch_for_ref("main")) == len(adapter.fetch_for_ref("anything"))


# --------------------------------------------------------------------------- read-only


def test_the_fetch_leaves_the_source_store_untouched(
    transcript_db: Path, adapter: HermesAdapter
) -> None:
    connection = sqlite3.connect(transcript_db.as_posix())
    try:
        before = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        connection.close()

    adapter.fetch_for_ref("main")
    adapter.health()

    connection = sqlite3.connect(transcript_db.as_posix())
    try:
        after = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        connection.close()

    assert after == before


def test_the_config_carries_no_secret_field() -> None:
    # A configuration object that could carry a credential is one that could leak one.
    assert set(AgentTranscriptConfig.model_fields) == {"db_path", "system_id"}


# --------------------------------------------------------------------------- the shared mapper


def test_the_base_mapper_is_agent_agnostic() -> None:
    """The base class maps any Turn to inputs without knowing the agent -- the whole template."""

    class _AnyAgent(AgentTranscriptAdapter):
        adapter_id = "demo-agent"

        @classmethod
        def default_db_path(cls) -> Path:
            raise NotImplementedError

        def iter_turns(self):  # pragma: no cover - not exercised here
            yield from ()

    config = AgentTranscriptConfig(db_path=Path("x"), system_id="demo")
    adapter = _AnyAgent(config)
    turn = Turn(
        turn_id="t1",
        session_id="sess1",
        occurred_at=datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
        statement="All checks are green after the change.",
        calls=(ToolCall(call_id="k1", tool="terminal", result="ok"),),
    )
    inputs = adapter.normalize(turn)
    events = [i for i in inputs if isinstance(i, EventInput)]
    claims = [i for i in inputs if isinstance(i, ClaimInput)]
    evidence = [i for i in inputs if isinstance(i, EvidenceInput)]
    assert len(events) == 1 and len(claims) == 1 and len(evidence) == 1
    assert events[0].payload["claim_id"] == claims[0].id
    assert evidence[0].relevance_to_claim == claims[0].id
    assert events[0].source_system_id == "demo"


def test_min_statement_length_is_a_positive_threshold() -> None:
    assert MIN_STATEMENT_LENGTH >= 1
