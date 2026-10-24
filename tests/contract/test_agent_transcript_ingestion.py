"""Contract test for the agent-transcript adapters (ADR 0010), SPEC §17.1 "each adapter".

Like ``tests/contract/test_git_ci_adapter.py``, this asserts the adapter's *observable
behaviour* against the real boundary, not its internals: its output is pushed through the real
``IngestionService`` against a migrated SQLite database, and the findings service is run over the
result. Three things make it a contract test rather than a unit test:

* **A real, migrated database** from the ``session_factory`` fixture, so the output is proven
  acceptable to the gateway -- redaction, schema validation, identity deduplication, and audit --
  not merely to a Pydantic model.
* **The whole fetch is ingested** the same way the watcher ingests it
  (``ingest_event`` / ``register_claim`` / ``register_evidence``), so the test exercises the real
  production path.
* **Idempotency and linkage are proven end to end**: a refetch re-ingests as duplicates, and the
  claim and evidence the adapter emitted resolve through the event the findings service reads.

The transcript store is a seeded database the test controls (see the unit test), so the result is
deterministic and does not depend on the live, personal ``state.db``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import nyx.critics  # noqa: F401 - importing the package registers the deterministic critic pack
from nyx.adapters.agent_transcript.config import AgentTranscriptConfig
from nyx.adapters.agent_transcript.hermes import HermesAdapter
from nyx.config import Settings
from nyx.domain.enums import CriticResultStatus
from nyx.findings.service import FindingService
from nyx.ingestion.inputs import ClaimInput, EventInput, EvidenceInput
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory

_MESSAGE_INSERT = (
    "INSERT INTO messages (id, session_id, role, content, tool_calls, tool_call_id, timestamp) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def _seed_hermes_db(path: Path) -> None:
    """Create a minimal Hermes-shaped store with one turn that has a claim and a tool result."""

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
        connection.execute(
            _MESSAGE_INSERT,
            (
                1,
                "s1",
                "assistant",
                "The tests all pass and the build is green.",
                '[{"call_id": "c2", "id": "c2", "function": {"name": "terminal", '
                '"arguments": {}}}]',
                None,
                1_792_000_300.0,
            ),
        )
        connection.execute(
            _MESSAGE_INSERT,
            (2, "s1", "tool", "648 passed in 12s", None, "c2", 1_792_000_301.0),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture()
def adapter(tmp_path: Path) -> HermesAdapter:
    transcript_db = tmp_path / "state.db"
    _seed_hermes_db(transcript_db)
    settings = Settings(agent_transcript_db=transcript_db.as_posix())
    return HermesAdapter(AgentTranscriptConfig.from_settings(settings, default_db=transcript_db))


def _ingest_all(service: IngestionService, inputs: list) -> None:
    """Ingest every input the way the watcher does, asserting each is accepted."""

    for position, item in enumerate(inputs):
        if isinstance(item, EventInput):
            result = service.ingest_event(item, request_id=f"contract-event-{position}")
        elif isinstance(item, ClaimInput):
            result = service.register_claim(item, request_id=f"contract-claim-{position}")
        elif isinstance(item, EvidenceInput):
            result = service.register_evidence(
                item.model_dump(mode="json"), request_id=f"contract-evidence-{position}"
            )
        else:  # pragma: no cover - the adapter only emits these three
            raise AssertionError(f"unexpected adapter output: {type(item).__name__}")
        assert result.status == "created", result.error


def test_every_input_is_accepted_by_the_real_gateway(
    adapter: HermesAdapter, service: IngestionService
) -> None:
    _ingest_all(service, adapter.fetch_for_ref("main"))


def test_a_refetch_reingests_as_duplicates_not_new_rows(
    adapter: HermesAdapter, service: IngestionService
) -> None:
    """Idempotency end to end: the stable identities make a second fetch a no-op."""

    first = adapter.fetch_for_ref("main")
    _ingest_all(service, first)

    second = adapter.fetch_for_ref("main")
    assert sorted(i.id for i in second) == sorted(i.id for i in first)

    duplicates = 0
    for position, item in enumerate(second):
        if isinstance(item, EventInput):
            result = service.ingest_event(item, request_id=f"dup-event-{position}")
        elif isinstance(item, ClaimInput):
            result = service.register_claim(item, request_id=f"dup-claim-{position}")
        else:
            result = service.register_evidence(
                item.model_dump(mode="json"), request_id=f"dup-evidence-{position}"
            )
        assert result.status == "duplicate", result.error
        duplicates += 1
    assert duplicates == len(second)


def test_the_claim_and_evidence_link_through_the_event(
    adapter: HermesAdapter, service: IngestionService, session_factory: SessionFactory
) -> None:
    """The event payload carries the claim and evidence ids, and the critic resolves them."""

    inputs = adapter.fetch_for_ref("main")
    _ingest_all(service, inputs)

    event = next(i for i in inputs if isinstance(i, EventInput))
    claim = next(i for i in inputs if isinstance(i, ClaimInput))
    evidence_ids = [i.id for i in inputs if isinstance(i, EvidenceInput)]

    # Storage-level linkage: the ids the adapter supplied are the ids that were stored.
    stored_event = service.get_event(event.id)
    assert stored_event is not None
    assert stored_event.payload["claim_id"] == claim.id
    assert set(stored_event.payload["evidence_ids"]) == set(evidence_ids)

    stored_claim = service.get_claim(claim.id)
    assert stored_claim is not None
    assert stored_claim.statement == "The tests all pass and the build is green."

    for evidence_id in evidence_ids:
        stored = service.get_evidence(evidence_id)
        assert stored is not None
        assert stored.relevance_to_claim == claim.id

    # The findings service resolves the same link when it builds the critic context: C-004's
    # proposition names the claim it was given, which only happens if the claim was found.
    outcomes = FindingService(session_factory).run_critics(event.id, ["c004-provenance-gap"])
    c004 = next(o for o in outcomes if o.critic_id == "c004-provenance-gap")
    assert c004.result is not CriticResultStatus.ERROR
    assert claim.id in c004.proposition


def test_the_event_occurred_at_comes_from_the_transcript_not_the_fetch(
    adapter: HermesAdapter, service: IngestionService
) -> None:
    """A stable, transcript-derived time keeps re-fetches idempotent; it is never the fetch time."""

    event = next(i for i in adapter.fetch_for_ref("main") if isinstance(i, EventInput))
    result = service.ingest_event(event, request_id="occurred-at")
    assert result.status == "created", result.error

    stored = service.get_event(result.record_id)
    assert stored is not None
    expected = datetime.fromtimestamp(1_792_000_300.0, tz=UTC)
    assert stored.occurred_at == expected


def test_the_adapter_emits_inputs_and_never_a_finding_itself(
    adapter: HermesAdapter, service: IngestionService, session_factory: SessionFactory
) -> None:
    """The adapter produces producer inputs; it never composes a finding (ADR 0004)."""

    from nyx.findings.service import FindingQuery

    _ingest_all(service, adapter.fetch_for_ref("main"))
    # No critics were run, so nothing has composed a finding from the adapter's inputs.
    page = FindingService(session_factory).list_findings(FindingQuery())
    assert page.items == []
