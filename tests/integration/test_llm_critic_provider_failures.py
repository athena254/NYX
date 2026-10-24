"""Scenario 19 with the real provider transport: every failure fails closed over a socket.

The transport is ``SyncCriticTransport`` over ``httpx`` against a local ``ThreadingHTTPServer``.
The server speaks the OpenAI wire shape and is configured per case; nothing here contacts a real
provider. Assertions read the migrated database back, so a run that returns ``ERROR`` while the
registry wrote a finding would fail this file.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from sqlalchemy import select

from nyx.critics import registry as critic_registry
from nyx.critics.llm import CRITIC_ID, LlmCritic
from nyx.critics.llm_transport import build_llm_critic
from nyx.critics.registry import register, run_selected
from nyx.ingestion.service import IngestionService
from nyx.llm.provider import LLMConfig
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.tables import CriticRunRow, FindingRow


class _ProviderHandler(BaseHTTPRequestHandler):
    """One OpenAI-shaped response per server, described by the server's ``plan``."""

    server_version = "provider-failure-test"

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        plan: dict[str, Any] = self.server.plan  # type: ignore[attr-defined]
        if plan.get("delay"):
            time.sleep(plan["delay"])
        body = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": plan.get("content", "")}}],
                "usage": plan.get("usage", {}),
            }
        ).encode("utf-8")
        try:
            self.send_response(int(plan.get("status", 200)))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return None

    def log_message(self, format: str, *args: object) -> None:
        return None


@contextmanager
def _provider(**plan: Any) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    httpd.plan = plan  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def restore_registry() -> Iterator[None]:
    original = dict(critic_registry._REGISTRY)
    try:
        yield
    finally:
        critic_registry._REGISTRY.clear()
        critic_registry._REGISTRY.update(original)


def _seed(service: IngestionService) -> tuple[str, str]:
    """Persist a system, claim, event, and one linked evidence item.

    Returns ``(event_id, evidence_id)`` so the happy path can cite the persisted id.
    """

    system = service.register_system(
        {
            "name": "llm-provider-failure-sys",
            "kind": "agent",
            "environment": "development",
            "owner": "test",
            "adapter": "http",
        },
        request_id="provider-sys",
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
            "producer_event_id": "provider-claim-1",
        },
        request_id="provider-claim",
    )
    assert claim.status == "created", claim.error
    assert claim.record_id is not None

    event = service.ingest_event(
        {
            "source_system_id": system_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-19T10:00:00Z",
            "correlation_id": "provider-corr-1",
            "payload": {"claim_id": claim.record_id},
            "producer_event_id": "provider-event-1",
            "trust_class": "observed",
            "labels": ["llm-provider"],
            "retention_class": "standard",
        },
        request_id="provider-event",
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
            "producer_event_id": "provider-evidence-0",
        },
        request_id="provider-evidence-0",
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
    base_url: str,
    timeout_seconds: float = 5.0,
) -> tuple[list[FindingRow], list[CriticRunRow]]:
    config = LLMConfig(
        protocol="openai",
        base_url=base_url,
        model="test-model",
        timeout_seconds=timeout_seconds,
    )
    register(build_llm_critic(config, provider="local"))
    with session_scope(factory) as session:
        outcomes = run_selected(session, event_id=event_id, critic_ids=[CRITIC_ID])
    assert len(outcomes) == 1

    with session_scope(factory) as session:
        findings = list(session.scalars(select(FindingRow)))
        runs = list(session.scalars(select(CriticRunRow)))
    return findings, runs


def _assert_failed_closed(
    findings: list[FindingRow],
    runs: list[CriticRunRow],
    *,
    expected: set[str],
) -> CriticRunRow:
    assert findings == [], "a failed provider call must not compose a finding"
    assert len(runs) == 1
    run = runs[0]
    assert run.result in expected
    assert run.finding_id is None
    assert run.model_provider == "local"
    assert run.model_version == "test-model"
    return run


def test_an_http_error_fails_closed(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    with _provider(status=500, content="upstream exploded") as base_url:
        findings, runs = _run(session_factory, event_id=event_id, base_url=base_url)

    _assert_failed_closed(findings, runs, expected={"error"})


def test_invalid_json_from_the_provider_fails_closed(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    with _provider(content="this is not JSON") as base_url:
        findings, runs = _run(session_factory, event_id=event_id, base_url=base_url)

    run = _assert_failed_closed(findings, runs, expected={"error"})
    assert "not valid JSON" in run.rationale


def test_empty_content_from_the_provider_fails_closed(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    with _provider(content="") as base_url:
        findings, runs = _run(session_factory, event_id=event_id, base_url=base_url)

    _assert_failed_closed(findings, runs, expected={"error"})


def test_a_provider_timeout_fails_closed(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    with _provider(content=_assessment(), delay=0.5) as base_url:
        started = time.monotonic()
        findings, runs = _run(
            session_factory,
            event_id=event_id,
            base_url=base_url,
            timeout_seconds=0.05,
        )
        elapsed = time.monotonic() - started

    _assert_failed_closed(findings, runs, expected={"error"})
    assert elapsed < 2.0, "the critic must stop at its deadline, not wait for the provider"


def test_a_model_refusal_is_inconclusive_not_a_finding(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    refusal = _assessment(
        result="inconclusive",
        observations=[],
        refusal="I cannot assess this evidence.",
    )
    with _provider(content=refusal) as base_url:
        findings, runs = _run(session_factory, event_id=event_id, base_url=base_url)

    _assert_failed_closed(findings, runs, expected={"inconclusive"})


def test_an_unlinked_evidence_id_is_inconclusive(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, _ = _seed(service)
    cited = _assessment(result="finding", supporting_evidence_ids=["ev_never_persisted"])
    with _provider(content=cited) as base_url:
        findings, runs = _run(session_factory, event_id=event_id, base_url=base_url)

    run = _assert_failed_closed(findings, runs, expected={"inconclusive"})
    assert "ev_never_persisted" in run.rationale


def test_a_valid_response_composes_a_finding_and_records_wire_usage(
    service: IngestionService,
    session_factory: SessionFactory,
    restore_registry: None,
) -> None:
    event_id, evidence_id = _seed(service)
    finding_payload = _assessment(
        result="finding",
        supporting_evidence_ids=[evidence_id],
        observations=["The linked CI evidence was reviewed."],
        category="other",
        severity="medium",
        title="Provider-proposed evidence gap",
        summary="The model proposed a finding citing one linked evidence item.",
    )
    with _provider(
        content=finding_payload,
        usage={"prompt_tokens": 111, "completion_tokens": 7},
    ) as base_url:
        findings, runs = _run(session_factory, event_id=event_id, base_url=base_url)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.status == "detected", "a model critic never advances a lifecycle state"
    assert list(finding.evidence_ids) == [evidence_id]

    assert len(runs) == 1
    run = runs[0]
    assert run.result == "finding"
    assert run.finding_id == finding.id
    assert run.resource_usage == {"input_tokens": 111.0, "output_tokens": 7.0}
    assert run.llm_io is None


def test_the_transport_is_what_the_registry_uses() -> None:
    """A guard against a future wiring that quietly constructs a second critic type."""

    from nyx.critics.llm_transport import SyncCriticTransport

    critic = build_llm_critic(
        LLMConfig(protocol="openai", base_url="http://127.0.0.1:9", model="m"),
        provider="local",
    )

    assert isinstance(critic, LlmCritic)
    assert isinstance(critic._transport, SyncCriticTransport)
