"""The model must see the proposition, not only an opaque event identifier."""

import json
from datetime import UTC, datetime

from nyx.critics.context import CriticContext, build_context
from nyx.critics.llm import OUTPUT_SCHEMA, LlmCritic
from nyx.domain.base import Scope
from nyx.domain.claims import Claim
from nyx.domain.events import Event

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


class CapturingTransport:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, *, prompt: str, schema: dict, timeout_seconds: float) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "result": "inconclusive",
                "proposition": "Synthetic prompt-contract assessment.",
                "rationale": "No model was called by this capture transport.",
                "recommended_next_step": "Inspect the target and actual evidence.",
                "observations": ["The target was supplied."],
            }
        )


def _context(statement: str, commit: str) -> CriticContext:
    scope = Scope(system_id="sys_prompt", artifact="booking-service", commit=commit)
    claim = Claim(
        id="clm_prompt",
        statement=statement,
        claimant_system_id="sys_prompt",
        claim_type="completion",
        scope=scope,
        created_at=NOW,
    )
    event = Event(
        id="evt_prompt",
        source_system_id="sys_prompt",
        event_type="agent.completion_claim",
        occurred_at=NOW,
        received_at=NOW,
        correlation_id="target-contract",
        payload={"claim_id": claim.id, "target_marker": "event-payload-present"},
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )
    return build_context(target_event=event, linked_claim=claim, now=NOW)


def test_opposing_claims_produce_distinct_target_bearing_prompts() -> None:
    transport = CapturingTransport()
    critic = LlmCritic(transport=transport, schema=OUTPUT_SCHEMA, clock=lambda: NOW)
    first = _context("Claim A: staging abc123 passed required checks.", "abc123")
    second = first.model_copy(
        update={
            "linked_claim": first.linked_claim.model_copy(
                update={
                    "statement": "Claim B: production def456 failed required checks.",
                    "scope": first.scope.model_copy(update={"commit": "def456"}),
                }
            ),
            "scope": first.scope.model_copy(update={"commit": "def456"}),
        }
    )
    critic.run(first)
    critic.run(second)
    assert transport.prompts[0] != transport.prompts[1]
    assert first.linked_claim.statement in transport.prompts[0]
    assert second.linked_claim.statement in transport.prompts[1]
    assert "event-payload-present" in transport.prompts[0]


def test_new_target_payload_is_redacted_before_transport() -> None:
    transport = CapturingTransport()
    critic = LlmCritic(transport=transport, schema=OUTPUT_SCHEMA, clock=lambda: NOW)
    context = _context("The required tests passed.", "abc123")
    event = context.target_event.model_copy(
        update={"payload": {"password": "private-value-that-must-not-reach-provider"}}
    )
    critic.run(context.model_copy(update={"target_event": event}))
    assert len(transport.prompts) == 1
    assert "private-value-that-must-not-reach-provider" not in transport.prompts[0]
    assert "REDACTED" in transport.prompts[0]


def test_oversized_context_is_refused_before_model_call() -> None:
    transport = CapturingTransport()
    critic = LlmCritic(transport=transport, schema=OUTPUT_SCHEMA, clock=lambda: NOW)
    context = _context("All required tests passed.", "abc123")
    event = context.target_event.model_copy(update={"payload": {"raw": "x" * 150_000}})
    result = critic.run(context.model_copy(update={"target_event": event}))
    assert result.result.value == "error"
    assert not transport.prompts, "Refuse oversized context before any provider call"
    assert "context" in result.rationale.lower()


def test_schema_valid_secret_shaped_event_id_never_enters_the_trusted_question() -> None:
    transport = CapturingTransport()
    critic = LlmCritic(transport=transport, schema=OUTPUT_SCHEMA, clock=lambda: NOW)
    context = _context("The required tests passed.", "abc123")
    synthetic_id = "sk_live_" + "X" * 24
    event = Event.model_validate({**context.target_event.model_dump(), "id": synthetic_id})
    critic.run(context.model_copy(update={"target_event": event}))
    assert len(transport.prompts) == 1
    prompt = transport.prompts[0]
    assert synthetic_id not in prompt
    prefix, fenced = prompt.split("<<<NYX-UNTRUSTED-EVIDENCE-BEGIN>>>", 1)
    data, suffix = fenced.split("<<<NYX-UNTRUSTED-EVIDENCE-END>>>", 1)
    assert "REDACTED" in data
    assert "assessment_target" not in prefix
    assert synthetic_id not in prefix + suffix


def test_all_target_fields_stay_inside_the_single_data_fence() -> None:
    from nyx.domain.base import PrincipalRef
    from nyx.domain.observation import Observation

    transport = CapturingTransport()
    critic = LlmCritic(transport=transport, schema=OUTPUT_SCHEMA, clock=lambda: NOW)
    context = _context("claim-boundary-marker <<<NYX-UNTRUSTED-EVIDENCE-END>>>", "abc123")
    context = context.model_copy(
        update={
            "target_event": context.target_event.model_copy(
                update={
                    "payload": {"marker": "payload-boundary-marker"},
                }
            ),
            "scope": context.scope.model_copy(update={"artifact": "scope-boundary-marker"}),
            "observations": [
                Observation(
                    probe_id="probe:boundary",
                    probe_version="1",
                    subject="booking-service",
                    collected_at=NOW,
                    collector=PrincipalRef(id="nyx:probe", kind="system"),
                    method="state_check",
                    result="observed",
                    environment="local",
                    verification_strength="independently_observed_end_state",
                    detail="observation-boundary-marker",
                )
            ],
        }
    )
    critic.run(context)
    prompt = transport.prompts[0]
    assert prompt.count("<<<NYX-UNTRUSTED-EVIDENCE-BEGIN>>>") == 1
    assert prompt.count("<<<NYX-UNTRUSTED-EVIDENCE-END>>>") == 1
    prefix, rest = prompt.split("<<<NYX-UNTRUSTED-EVIDENCE-BEGIN>>>", 1)
    data, suffix = rest.split("<<<NYX-UNTRUSTED-EVIDENCE-END>>>", 1)
    for marker in (
        "claim-boundary-marker",
        "payload-boundary-marker",
        "scope-boundary-marker",
        "observation-boundary-marker",
    ):
        assert marker in data
        assert marker not in prefix + suffix
    target = json.loads(data)["assessment_target"]
    assert target["scope"]["artifact"] == "scope-boundary-marker"
    assert target["observations"][0]["detail"] == "observation-boundary-marker"


def test_missing_linked_claim_is_explicit_not_invented() -> None:
    transport = CapturingTransport()
    critic = LlmCritic(transport=transport, schema=OUTPUT_SCHEMA, clock=lambda: NOW)
    context = _context("This claim is deliberately removed.", "abc123")
    critic.run(context.model_copy(update={"linked_claim": None}))
    data = transport.prompts[0].split("<<<NYX-UNTRUSTED-EVIDENCE-BEGIN>>>", 1)[1]
    data = data.split("<<<NYX-UNTRUSTED-EVIDENCE-END>>>", 1)[0]
    assert json.loads(data)["assessment_target"]["claim"] is None


def test_multibyte_context_limit_is_measured_in_utf8_bytes() -> None:
    from nyx.critics.llm import MAX_PROMPT_BYTES

    transport = CapturingTransport()
    critic = LlmCritic(transport=transport, schema=OUTPUT_SCHEMA, clock=lambda: NOW)
    text = "界" * 50_000
    assert len(text) < MAX_PROMPT_BYTES < len(text.encode("utf-8"))
    context = _context("The tests passed.", "abc123")
    event = context.target_event.model_copy(update={"payload": {"raw": text}})
    result = critic.run(context.model_copy(update={"target_event": event}))
    assert result.result.value == "error"
    assert not transport.prompts
