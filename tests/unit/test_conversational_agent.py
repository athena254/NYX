"""The chat agent: the model drives the tool surface, and cannot judge.

The claim this file exists to test is the one the whole conversational layer rests on:

> **The language model is the interface, never the judge.**

That is enforced by the *tool surface* -- there is no tool that creates a finding, records a
disposition, or changes a lifecycle state. These tests prove it from three directions: the tool
specs the agent offers, the guard that refuses to run if one appears, and the refusal a model
actually receives when it tries to call one.

A fake LLM stands in for the provider, so these tests are deterministic and do not spend money or
depend on a model being reachable. The live path is covered by ``scripts/probe_chat_live.py``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from nyx.conversational.agent import (
    FORBIDDEN_TOOLS,
    JUDGE_ONLY_OPERATIONS,
    SYSTEM_PROMPT,
    ChatAgent,
    ChatError,
    assert_no_judge_tools,
    build_llm_config,
    tool_specs,
)
from nyx.conversational.client import NyxClient
from nyx.conversational.llm import (
    ChatMessage,
    LLMError,
    LLMErrorCode,
    LLMResponse,
    ToolCall,
)


class _FakeLLM:
    """A provider double: returns a scripted response, records what it was asked."""

    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[ChatMessage], list | None]] = []

    async def chat(self, messages, tools=None):  # type: ignore[no-untyped-def]
        self.calls.append((list(messages), tools))
        if not self._responses:
            raise AssertionError("the fake LLM ran out of scripted responses")
        return self._responses.pop(0)

    @property
    def tools_offered(self) -> list[str]:
        _, tools = self.calls[0]
        return [spec.name for spec in tools or []]


def _text_response(content: str) -> LLMResponse:
    return LLMResponse(content=content)


def _tool_response(name: str, arguments: dict, call_id: str = "call_1") -> LLMResponse:
    return LLMResponse(
        content="", tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)]
    )


def _agent(handler, llm, *, actor: str = "tester") -> ChatAgent:
    return ChatAgent(
        llm,
        NyxClient("http://nyx.test", transport=httpx.MockTransport(handler)),
        actor=actor,
    )


def _ok(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)


# --------------------------------------------------------------------------- the safety property


def test_the_agent_offers_no_judge_capable_tool() -> None:
    """The model is never handed a tool that could produce a verdict."""

    fake = _FakeLLM(_text_response("done"))
    agent = _agent(lambda _r: _ok({"items": []}), fake)

    agent.run("hello")

    offered = fake.tools_offered
    assert offered, "the agent offered the model no tools at all"
    assert set(offered) & set(FORBIDDEN_TOOLS) == set()
    for spec in tool_specs():
        assert spec.name not in FORBIDDEN_TOOLS


def test_a_judge_capable_tool_is_refused_before_it_runs() -> None:
    """Even if a model somehow asks for one, the call is refused with a reason."""

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["called"] = True
        return _ok({"items": []})

    fake = _FakeLLM(
        _tool_response("nyx_create_finding", {"title": "unsafe"}),
        _text_response("I cannot create a finding."),
    )
    agent = _agent(handler, fake)

    answer = agent.run("create a finding")

    assert seen.get("called") is not True, "NYX was contacted for a forbidden operation"
    # The refusal is what the model read, in the tool result it was handed.
    tool_message = [m for m in fake.calls[1][0] if m.role == "tool"][0]
    assert "TOOL_FORBIDDEN" in json.loads(tool_message.content)["error"]
    assert answer == "I cannot create a finding."


def test_a_malformed_tool_call_is_reported_not_raised() -> None:
    """A model that omits a required argument gets a result it can correct, not a crash.

    Found while writing this suite: the handler raised ``KeyError`` on a missing argument, so a
    single malformed call aborted the turn instead of letting the model try again.
    """

    fake = _FakeLLM(
        _tool_response("nyx_submit_rebuttal", {"finding_id": "fnd_1"}),
        _text_response("I need the argument text."),
    )
    agent = _agent(lambda _r: _ok({"id": "rbt_1"}), fake)

    answer = agent.run("challenge it")

    assert answer == "I need the argument text."
    tool_message = [m for m in fake.calls[1][0] if m.role == "tool"][0]
    payload = json.loads(tool_message.content)
    assert payload["error"] == "INVALID_ARGUMENTS"
    assert "argument" in payload["message"]


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_TOOLS))
def test_assert_no_judge_tools_rejects_each_forbidden_id(forbidden: str) -> None:
    with pytest.raises(ChatError) as excinfo:
        assert_no_judge_tools(["nyx_list_findings", forbidden])

    assert excinfo.value.kind == "unsafe_tool_surface"


def test_assert_no_judge_tools_accepts_the_real_surface() -> None:
    assert_no_judge_tools([spec.name for spec in tool_specs()])


def test_a_judge_capable_operation_is_refused_even_under_another_name() -> None:
    """The check is semantic, not just a literal id match."""

    for _name, operation in (
        ("nyx_record_disposition", "record_disposition"),
        ("nyx_transition_finding", "transition_finding"),
        ("nyx_accept_risk", "accept_risk"),
        ("nyx_set_finding_status", "set_finding_status"),
        ("nyx_create_finding", "create_finding"),
    ):
        assert operation in JUDGE_ONLY_OPERATIONS, f"{operation} is not guarded"


def test_the_system_prompt_forbids_promising_a_verdict() -> None:
    """The prompt must close the gap where a model offers to create a finding it cannot."""

    assert "no tool that creates a finding" in SYSTEM_PROMPT
    assert "Never promise to create one" in SYSTEM_PROMPT
    assert "records a disposition" in SYSTEM_PROMPT


# --------------------------------------------------------------------------- tool results


def test_a_tool_result_is_passed_back_to_the_model() -> None:
    """The model must see NYX's answer, not just its own request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/systems"):
            return _ok({"items": [{"id": "sys_1", "name": "ghost-factory"}]})
        return _ok({"items": []})

    fake = _FakeLLM(
        _tool_response("nyx_list_systems", {}),
        _text_response("NYX monitors ghost-factory (sys_1)."),
    )
    agent = _agent(handler, fake)

    answer = agent.run("what systems does NYX monitor?")

    assert answer == "NYX monitors ghost-factory (sys_1)."
    second_messages, _ = fake.calls[1]
    tool_messages = [m for m in second_messages if m.role == "tool"]
    assert len(tool_messages) == 1
    payload = json.loads(tool_messages[0].content)
    assert payload["result"][0]["id"] == "sys_1"


def test_a_nyx_refusal_reaches_the_model_as_a_result() -> None:
    """A refusal is data, not a crash: the model can report it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"code": "CAPABILITY_REQUIRED", "message": "principal lacks the capability"},
        )

    fake = _FakeLLM(
        _tool_response(
            "nyx_request_verification",
            {"finding_id": "fnd_1", "check_id": "critics.rerun-linked", "requested_by": "t"},
        ),
        _text_response("NYX refused the request."),
    )
    agent = _agent(handler, fake)

    agent.run("verify it")

    tool_message = [m for m in fake.calls[1][0] if m.role == "tool"][0]
    payload = json.loads(tool_message.content)
    assert payload["error"] == "CAPABILITY_REQUIRED"
    assert payload["status"] == 403


def test_the_agent_records_what_the_session_touched() -> None:
    """A user must be able to see the blast radius of a conversation."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rebuttals"):
            return _ok({"id": "rbt_1"})
        return _ok({"items": []})

    fake = _FakeLLM(
        _tool_response(
            "nyx_submit_rebuttal",
            {"finding_id": "fnd_1", "submitted_by": "t", "argument": "stale"},
        ),
        _text_response("filed"),
    )
    agent = _agent(handler, fake)

    agent.run("challenge finding fnd_1")

    assert agent.touched == ["fnd_1"]


def test_the_model_cannot_exceed_the_tool_round_budget() -> None:
    """A runaway model loop stops instead of running forever."""

    fake = _FakeLLM(*[_tool_response("nyx_list_findings", {}) for _ in range(20)])
    agent = _agent(
        lambda _r: _ok({"items": []}),
        fake,
    )

    with pytest.raises(ChatError) as excinfo:
        agent.run("loop forever")

    assert excinfo.value.kind == "too_many_rounds"


def test_a_model_that_never_settles_is_reported_not_retried_silently() -> None:
    """The failure mode is an explicit error, never a fabricated answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"items": []})

    fake = _FakeLLM(*[_tool_response("nyx_list_findings", {}, call_id=f"c{i}") for i in range(30)])
    agent = _agent(handler, fake)

    with pytest.raises(ChatError):
        agent.run("loop")


def test_a_model_error_is_reported_as_unavailable() -> None:
    """A provider outage must never become a made-up answer."""

    class _Broken:
        async def chat(self, messages, tools=None):  # type: ignore[no-untyped-def]
            raise LLMError(
                "connection refused",
                code=LLMErrorCode.UNAVAILABLE,
            )

    agent = ChatAgent(_Broken(), NyxClient("http://nyx.test"), actor="tester")  # type: ignore[arg-type]

    with pytest.raises(ChatError) as excinfo:
        agent.run("hello")

    assert excinfo.value.kind == "model_unavailable"
    assert "no answer was fabricated" in str(excinfo.value)


def test_session_memory_survives_between_turns() -> None:
    """The conversation is retained, so follow-up questions have context."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"items": []})

    fake = _FakeLLM(_text_response("first"), _text_response("second"))
    agent = _agent(handler, fake)

    first = agent.run("first question")
    second = agent.run("follow-up")

    assert (first, second) == ("first", "second")
    # The second turn is sent with the whole first exchange behind it.
    assert "first question" in [m.content for m in agent.history if m.role == "user"]
    assert "follow-up" in [m.content for m in agent.history if m.role == "user"]
    assert agent.history[0].role == "system"
    # Two user turns, two assistant answers, in order.
    assert [m.role for m in agent.history] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_the_transcript_omits_the_system_prompt() -> None:
    fake = _FakeLLM(_text_response("hi"))
    agent = _agent(lambda _r: _ok({"items": []}), fake)
    agent.run("hello")

    lines = list(agent.transcript())

    assert lines
    assert all(not line.startswith("system") for line in lines)


def test_build_llm_config_refuses_to_guess_a_model() -> None:
    with pytest.raises(ChatError) as excinfo:
        build_llm_config(model="")

    assert excinfo.value.kind == "model_unavailable"


def test_build_llm_config_carries_the_protocol_and_model() -> None:
    config = build_llm_config(
        protocol="anthropic", base_url="https://example.invalid", model="claude-x"
    )

    assert config.protocol == "anthropic"
    assert config.model == "claude-x"
    assert config.base_url == "https://example.invalid"


def test_the_actor_identity_is_threaded_not_defaulted() -> None:
    """AGENTS.md: never hardcode an actor. The caller's identity must reach NYX."""

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return _ok({"id": "rbt_1"})

    fake = _FakeLLM(
        _tool_response(
            "nyx_submit_rebuttal",
            {"finding_id": "fnd_1", "submitted_by": "alice", "argument": "stale"},
        ),
        _text_response("filed"),
    )
    agent = _agent(handler, fake, actor="alice")

    agent.run("challenge it")

    assert seen["body"]["submitted_by"] == "alice"


def test_submit_rebuttal_uses_the_requested_outcome() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return _ok({"id": "rbt_1"})

    fake = _FakeLLM(
        _tool_response(
            "nyx_submit_rebuttal",
            {"finding_id": "fnd_1", "argument": "stale", "requested_outcome": "withdrawn"},
        ),
        _text_response("filed"),
    )
    agent = _agent(handler, fake, actor="alice")
    agent.run("withdraw it")

    assert seen["body"]["requested_outcome"] == "withdrawn"
