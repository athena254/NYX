"""Acceptance scenario 19: a failing LLM critic does not corrupt finding state.

The assertions are deliberately on the **database**, not on the returned ``CriticResult``. A
critic that returns ``ERROR`` while the registry/composer still wrote a finding would pass a
return-value-only test and corrupt state in production. Every failure mode below therefore
reads ``findings`` and ``critic_runs`` back from a real migrated SQLite database.

The transport is a local fake. Nothing here touches a network or a provider.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from nyx.critics import registry as critic_registry
from nyx.critics.llm import (
    CRITIC_ID,
    OUTPUT_SCHEMA,
    LlmCritic,
    LlmTimeoutError,
    LlmTransportError,
)
from nyx.critics.registry import register, run_selected
from nyx.ingestion.injection import UNTRUSTED_CONTENT_NOTICE
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.tables import CriticRunRow, FindingRow


class _FakeTransport:
    """A transport that returns a canned response or raises a canned error."""

    def __init__(self, *, response: str | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, prompt: str, schema: dict[str, Any], timeout_seconds: float) -> str:
        self.calls.append({"prompt": prompt, "schema": schema, "timeout_seconds": timeout_seconds})
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@pytest.fixture()
def restore_registry() -> Iterator[None]:
    """Undo the test registration so the global critic pack is not polluted."""

    original = dict(critic_registry._REGISTRY)
    try:
        yield
    finally:
        critic_registry._REGISTRY.clear()
        critic_registry._REGISTRY.update(original)


def _seed(service: IngestionService) -> tuple[str, list[str]]:
    """Persist a system, claim, event, and two linked evidence items.

    Returns ``(event_id, evidence_ids)``. Two items are used so the positive case can prove that
    the composed finding cites *only* the evidence the model referenced.
    """

    system = service.register_system(
        {
            "name": "llm-critic-sys",
            "kind": "agent",
            "environment": "development",
            "owner": "test",
            "adapter": "http",
        },
        request_id="llm-sys",
    )
    assert system.status == "created", system.error
    assert system.record_id is not None
    system_id = system.record_id

    claim = service.register_claim(
        {
            "statement": "All required tests pass for commit abc123.",
            "claimant_system_id": system_id,
            "claim_type": "completion",
            "scope": {"system_id": system_id, "artifact": "demo", "commit": "abc123"},
            "producer_event_id": "llm-claim-1",
        },
        request_id="llm-claim",
    )
    assert claim.status == "created", claim.error
    assert claim.record_id is not None
    claim_id = claim.record_id

    event = service.ingest_event(
        {
            "source_system_id": system_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-19T10:00:00Z",
            "correlation_id": "llm-corr-1",
            "payload": {"claim_id": claim_id},
            "producer_event_id": "llm-event-1",
            "trust_class": "observed",
            "labels": ["llm-critic"],
            "retention_class": "standard",
        },
        request_id="llm-event",
    )
    assert event.status == "created", event.error
    assert event.record_id is not None

    evidence_ids: list[str] = []
    for index, commit in enumerate(("abc123", "def456")):
        evidence = service.register_evidence(
            {
                "kind": "ci_test_result",
                "title": f"CI run {index}",
                "description": f"CI run for commit {commit}: all checks passed.",
                "source_ref": f"https://ci.example.invalid/run/{index}",
                "source_system_id": system_id,
                "scope": {
                    "system_id": system_id,
                    "artifact": "demo",
                    "commit": commit,
                },
                "captured_at": "2026-10-19T09:00:00Z",
                "valid_from": "2026-10-19T09:00:00Z",
                "integrity_method": "none",
                "relevance_to_claim": claim_id,
                "limitations": ["Synthetic test evidence."],
                "sensitivity": "internal",
                "access_policy_ref": "test",
                "retention_class": "standard",
                "producer_event_id": f"llm-evidence-{index}",
            },
            request_id=f"llm-evidence-{index}",
        )
        assert evidence.status == "created", evidence.error
        assert evidence.record_id is not None
        evidence_ids.append(evidence.record_id)

    return event.record_id, evidence_ids


def _assessment(**overrides: Any) -> dict[str, Any]:
    """A schema-valid baseline response that a test can mutate into a failure mode."""

    payload: dict[str, Any] = {
        "result": "finding",
        "proposition": "The linked evidence contains a discrepancy worth review.",
        "rationale": "The model reports a discrepancy among the linked evidence records.",
        "recommended_next_step": "Ask an authorised human to review the cited evidence.",
        "observations": ["The linked evidence records were reviewed."],
        "supporting_evidence_ids": [],
        "counterevidence_ids": [],
        "limitations": ["Model output is an assessment, not verification."],
    }
    payload.update(overrides)
    return payload


def _run(
    factory: SessionFactory,
    *,
    event_id: str,
    transport: _FakeTransport | None,
    timeout_seconds: float | None = None,
) -> tuple[list[FindingRow], list[CriticRunRow]]:
    """Run only the LLM critic through the real registry, then read the database back."""

    critic = (
        LlmCritic(transport=transport, schema=OUTPUT_SCHEMA)
        if timeout_seconds is None
        else LlmCritic(transport=transport, schema=OUTPUT_SCHEMA, timeout_seconds=timeout_seconds)
    )
    register(critic)
    with session_scope(factory) as session:
        outcomes = run_selected(session, event_id=event_id, critic_ids=[CRITIC_ID])
    assert len(outcomes) == 1

    with session_scope(factory) as session:
        findings = list(session.scalars(select(FindingRow)))
        runs = list(session.scalars(select(CriticRunRow)))
    return findings, runs


def _assert_no_finding(
    findings: list[FindingRow],
    runs: list[CriticRunRow],
    *,
    expected_results: set[str],
) -> None:
    """The core scenario-19 assertion: the finding store is untouched."""

    assert findings == [], "a failed LLM critic must not write a finding"
    assert len(runs) == 1, "the run itself is recorded exactly once"
    assert runs[0].critic_id == CRITIC_ID
    assert runs[0].critic_type == "llm"
    assert runs[0].result in expected_results
    assert runs[0].finding_id is None, "a failed run must not point at a finding"


def test_timeout_writes_no_finding(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    transport = _FakeTransport(error=LlmTimeoutError("deadline exceeded"))

    findings, runs = _run(session_factory, event_id=event_id, transport=transport)

    _assert_no_finding(findings, runs, expected_results={"error"})
    assert transport.calls, "the critic must actually attempt the transport"


def test_the_configured_timeout_reaches_the_transport(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    """Phase 6 wiring: the critic and its provider share one deadline.

    The Phase 4 critic passed a module constant, which made the configured provider timeout
    invisible to the model call. The constructor now owns it.
    """

    event_id, _ = _seed(service)
    transport = _FakeTransport(response=json.dumps(_assessment(result="no_finding")))

    _run(
        session_factory,
        event_id=event_id,
        transport=transport,
        timeout_seconds=12.5,
    )

    assert transport.calls
    assert transport.calls[0]["timeout_seconds"] == 12.5


def test_invalid_json_writes_no_finding(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    transport = _FakeTransport(response="this is not json")

    findings, runs = _run(session_factory, event_id=event_id, transport=transport)

    _assert_no_finding(findings, runs, expected_results={"error"})


def test_schema_violation_writes_no_finding(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    payload = _assessment()
    del payload["rationale"]
    transport = _FakeTransport(response=json.dumps(payload))

    findings, runs = _run(session_factory, event_id=event_id, transport=transport)

    _assert_no_finding(findings, runs, expected_results={"error"})


def test_refusal_writes_no_finding(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    payload = _assessment(
        result="inconclusive",
        observations=[],
        refusal="I cannot assess this evidence.",
    )
    transport = _FakeTransport(response=json.dumps(payload))

    findings, runs = _run(session_factory, event_id=event_id, transport=transport)

    _assert_no_finding(findings, runs, expected_results={"inconclusive"})


def test_unknown_evidence_id_writes_no_finding(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    payload = _assessment(supporting_evidence_ids=["ev_never_persisted"])
    transport = _FakeTransport(response=json.dumps(payload))

    findings, runs = _run(session_factory, event_id=event_id, transport=transport)

    _assert_no_finding(findings, runs, expected_results={"inconclusive"})
    assert "ev_never_persisted" in runs[0].rationale


def test_missing_transport_fails_closed(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)

    findings, runs = _run(session_factory, event_id=event_id, transport=None)

    _assert_no_finding(findings, runs, expected_results={"error"})


def test_transport_error_writes_no_finding(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    transport = _FakeTransport(error=LlmTransportError("provider unavailable"))

    findings, runs = _run(session_factory, event_id=event_id, transport=transport)

    _assert_no_finding(findings, runs, expected_results={"error"})


def test_valid_output_composes_a_finding_citing_only_the_referenced_evidence(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, evidence_ids = _seed(service)
    referenced_id, unreferenced_id = evidence_ids
    payload = _assessment(
        result="finding",
        supporting_evidence_ids=[referenced_id],
        category="other",
        severity="medium",
        title="LLM-proposed evidence gap",
        summary="The model proposed a finding citing one linked evidence item.",
    )
    transport = _FakeTransport(response=json.dumps(payload))

    findings, runs = _run(session_factory, event_id=event_id, transport=transport)

    assert len(findings) == 1, "valid structured output must compose exactly one finding"
    finding = findings[0]
    assert list(finding.evidence_ids) == [referenced_id]
    assert list(finding.supporting_evidence_ids) == [referenced_id]
    assert list(finding.counterevidence_ids) == []
    assert unreferenced_id not in finding.evidence_ids
    assert finding.status == "detected", "an LLM critic never advances a lifecycle state"

    assert len(runs) == 1
    assert runs[0].result == "finding"
    assert runs[0].finding_id == finding.id
    assert transport.calls, "the critic must have called the transport"
    assert UNTRUSTED_CONTENT_NOTICE in transport.calls[0]["prompt"]
