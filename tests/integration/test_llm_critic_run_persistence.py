"""Phase 6: a model critic run persists its provenance and redacted I/O.

Every assertion here reads the migrated database back. The point of the slice is that a reader
of a finding can tell *which* model and *which* prompt template produced the assessment, and
that an enabled prompt log is still passed through redaction before it is stored.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import inspect, select

from nyx.critics import registry as critic_registry
from nyx.critics.llm import (
    CRITIC_ID,
    OUTPUT_SCHEMA,
    PROMPT_TEMPLATE_VERSION,
    LlmCritic,
    LlmTimeoutError,
)
from nyx.critics.registry import register, run_selected
from nyx.ingestion.injection import UNTRUSTED_CONTENT_NOTICE
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.critic_runs import CriticRunRepository
from nyx.storage.tables import CriticRunRow, FindingRow

_SECRET = "sk_live_abc123456789"  # noqa: S105 - synthetic value, asserted to be redacted


class _FakeTransport:
    """A local transport that returns canned text and reports token usage."""

    def __init__(
        self,
        *,
        response: str | None = None,
        error: Exception | None = None,
        usage: dict[str, int | None] | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.last_usage = usage
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, prompt: str, schema: dict[str, Any], timeout_seconds: float) -> str:
        self.calls.append({"prompt": prompt, "schema": schema, "timeout_seconds": timeout_seconds})
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@pytest.fixture()
def restore_registry() -> Iterator[None]:
    original = dict(critic_registry._REGISTRY)
    try:
        yield
    finally:
        critic_registry._REGISTRY.clear()
        critic_registry._REGISTRY.update(original)


def _seed(service: IngestionService) -> tuple[str, str]:
    """Persist one system, claim, event, and linked evidence item."""

    system = service.register_system(
        {
            "name": "llm-provenance-sys",
            "kind": "agent",
            "environment": "development",
            "owner": "test",
            "adapter": "http",
        },
        request_id="llm-prov-sys",
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
            "producer_event_id": "llm-prov-claim-1",
        },
        request_id="llm-prov-claim",
    )
    assert claim.status == "created", claim.error
    assert claim.record_id is not None

    event = service.ingest_event(
        {
            "source_system_id": system_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-19T10:00:00Z",
            "correlation_id": "llm-prov-corr-1",
            "payload": {"claim_id": claim.record_id},
            "producer_event_id": "llm-prov-event-1",
            "trust_class": "observed",
            "labels": ["llm-provenance"],
            "retention_class": "standard",
        },
        request_id="llm-prov-event",
    )
    assert event.status == "created", event.error
    assert event.record_id is not None

    evidence = service.register_evidence(
        {
            "kind": "ci_test_result",
            "title": "CI run for abc123",
            "description": "CI run for commit abc123: all checks passed.",
            "source_ref": "https://ci.example.invalid/run/0",
            "source_system_id": system_id,
            "scope": {"system_id": system_id, "artifact": "demo", "commit": "abc123"},
            "captured_at": "2026-10-19T09:00:00Z",
            "valid_from": "2026-10-19T09:00:00Z",
            "integrity_method": "none",
            "relevance_to_claim": claim.record_id,
            "limitations": ["Synthetic test evidence."],
            "sensitivity": "internal",
            "access_policy_ref": "test",
            "retention_class": "standard",
            "producer_event_id": "llm-prov-evidence-0",
        },
        request_id="llm-prov-evidence-0",
    )
    assert evidence.status == "created", evidence.error
    assert evidence.record_id is not None
    return event.record_id, evidence.record_id


def _assessment(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "result": "no_finding",
        "proposition": "No issue was found within the critic's declared scope.",
        "rationale": "The linked evidence was reviewed and no issue was identified.",
        "recommended_next_step": "No action is required; absence of a finding is not proof.",
        "observations": ["The linked CI evidence was reviewed."],
        "supporting_evidence_ids": [],
        "counterevidence_ids": [],
        "limitations": ["Model output is an assessment, not verification."],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _run(
    factory: SessionFactory,
    *,
    event_id: str,
    transport: _FakeTransport,
    model_provider: str = "local",
    model_version: str = "test-model",
    log_prompts: bool = False,
) -> list[CriticRunRow]:
    critic = LlmCritic(
        transport=transport,
        schema=OUTPUT_SCHEMA,
        model_provider=model_provider,
        model_version=model_version,
        log_prompts=log_prompts,
    )
    register(critic)
    with session_scope(factory) as session:
        outcomes = run_selected(session, event_id=event_id, critic_ids=[CRITIC_ID])
    assert len(outcomes) == 1

    with session_scope(factory) as session:
        return list(session.scalars(select(CriticRunRow)))


def test_a_run_records_provider_model_prompt_version_and_usage(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    transport = _FakeTransport(
        response=_assessment(),
        usage={"input_tokens": 12, "output_tokens": 7},
    )

    runs = _run(session_factory, event_id=event_id, transport=transport)

    assert len(runs) == 1
    run = runs[0]
    assert run.model_provider == "local"
    assert run.model_version == "test-model"
    assert run.prompt_version == PROMPT_TEMPLATE_VERSION
    assert run.resource_usage == {"input_tokens": 12.0, "output_tokens": 7.0}
    assert run.llm_io is None, "prompt logging is opt-in and off by default"

    with session_scope(session_factory) as session:
        domain = CriticRunRepository(session).get(run.id)
    assert domain is not None
    assert domain.model_provider == "local"
    assert domain.model_version == "test-model"
    assert domain.prompt_version == PROMPT_TEMPLATE_VERSION
    assert domain.resource_usage == {"input_tokens": 12.0, "output_tokens": 7.0}


def test_prompt_logging_stores_a_redacted_prompt_and_response(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    transport = _FakeTransport(
        response=_assessment(
            observations=[f"The model echoed api_key={_SECRET} from the untrusted data."]
        )
    )

    runs = _run(
        session_factory,
        event_id=event_id,
        transport=transport,
        log_prompts=True,
    )

    assert len(runs) == 1
    io = runs[0].llm_io
    assert io is not None
    assert "prompt" in io and "response" in io
    assert UNTRUSTED_CONTENT_NOTICE in io["prompt"]
    assert f"Prompt template version: {PROMPT_TEMPLATE_VERSION}" in io["prompt"]
    stored = json.dumps(io)
    assert _SECRET not in stored, "the persistence boundary must redact model output"


def test_a_failed_call_still_records_provenance_and_no_finding(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    transport = _FakeTransport(error=LlmTimeoutError("deadline exceeded"))

    runs = _run(
        session_factory,
        event_id=event_id,
        transport=transport,
        log_prompts=True,
    )

    assert len(runs) == 1
    run = runs[0]
    assert run.result == "error"
    assert run.finding_id is None
    assert run.model_provider == "local"
    assert run.model_version == "test-model"
    assert run.prompt_version == PROMPT_TEMPLATE_VERSION
    assert run.resource_usage is None, "a failed call measured nothing"
    assert run.llm_io is not None
    assert "response" not in run.llm_io

    with session_scope(session_factory) as session:
        assert list(session.scalars(select(FindingRow))) == []


def test_the_migration_adds_the_provenance_columns(engine: Any) -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("critic_runs")}

    assert {"model_provider", "prompt_version", "llm_io"} <= columns
