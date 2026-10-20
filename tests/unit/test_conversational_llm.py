"""The LLM adapter speaks both protocols without ever contacting a real provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nyx.conversational.llm import (
    AnthropicClient,
    ChatMessage,
    LLMClient,
    LLMConfig,
    LLMError,
    LLMErrorCode,
    LLMResponse,
    OpenAICompatibleClient,
    ToolSpec,
    build_client,
)

Handler = Callable[[httpx.Request], httpx.Response]
_KEY = "sk-test-distinctive-key-9f3a"

_TOOL = ToolSpec(
    name="get_finding",
    description="Fetch one finding by id.",
    parameters={
        "type": "object",
        "properties": {"finding_id": {"type": "string"}},
        "required": ["finding_id"],
    },
)


def _client(
    protocol: str,
    handler: Handler,
    *,
    api_key: str | None = None,
    max_retries: int = 0,
) -> LLMClient:
    transport = httpx.MockTransport(handler)
    if protocol == "anthropic":
        config = LLMConfig(
            protocol="anthropic",
            api_key=api_key,
            max_retries=max_retries,
        )
        return AnthropicClient(config, transport=transport)
    config = LLMConfig(protocol="openai", api_key=api_key, max_retries=max_retries)
    return OpenAICompatibleClient(config, transport=transport)


def _openai_tool_payload() -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_finding",
                                "arguments": json.dumps({"finding_id": "fnd_1", "limit": 2}),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_openai_parses_tool_calls_with_parsed_arguments() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_openai_tool_payload())

    client = _client("openai", handler, api_key="test-key")
    response = asyncio.run(
        client.chat([ChatMessage(role="user", content="What is fnd_1?")], tools=[_TOOL])
    )

    assert response.content == ""
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "get_finding"
    assert call.arguments == {"finding_id": "fnd_1", "limit": 2}
    assert isinstance(call.arguments, dict)
    assert response.input_tokens == 10
    assert response.output_tokens == 5

    assert str(requests[0].url) == "http://127.0.0.1:3000/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-key"
    sent = json.loads(requests[0].content)
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_finding",
                "description": "Fetch one finding by id.",
                "parameters": _TOOL.parameters,
            },
        }
    ]


def test_openai_plain_text_response_has_content_and_no_tool_calls() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hello."}}]})

    client = _client("openai", handler)
    response = asyncio.run(client.chat([ChatMessage(role="user", content="Hi")]))

    assert response.content == "Hello."
    assert response.tool_calls == []
    assert response.input_tokens is None
    assert response.output_tokens is None


def test_openai_arguments_that_look_like_a_dict_still_become_a_dict() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openai_tool_payload())

    client = _client("openai", handler)
    response = asyncio.run(client.chat([ChatMessage(role="user", content="Go")]))

    assert isinstance(response.tool_calls[0].arguments, dict)
    assert response.tool_calls[0].arguments["finding_id"] == "fnd_1"


def test_anthropic_tool_use_block_becomes_a_tool_call_taking_arguments_from_input() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_finding",
                        "input": {"finding_id": "fnd_1"},
                    },
                ],
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        )

    client = _client("anthropic", handler, api_key="test-key")
    response = asyncio.run(
        client.chat([ChatMessage(role="user", content="What is fnd_1?")], tools=[_TOOL])
    )

    assert response.content == "Checking."
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.id == "toolu_1"
    assert call.name == "get_finding"
    assert call.arguments == {"finding_id": "fnd_1"}
    assert response.input_tokens == 12
    assert response.output_tokens == 3

    assert str(requests[0].url) == "http://127.0.0.1:3000/v1/messages"
    assert requests[0].headers["x-api-key"] == "test-key"
    assert requests[0].headers["anthropic-version"] == "2023-06-01"
    sent = json.loads(requests[0].content)
    assert sent["tools"] == [
        {
            "name": "get_finding",
            "description": "Fetch one finding by id.",
            "input_schema": _TOOL.parameters,
        }
    ]


def test_anthropic_text_only_response_has_content_and_no_tool_calls() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "It is fine."}]})

    client = _client("anthropic", handler)
    response = asyncio.run(client.chat([ChatMessage(role="user", content="Well?")]))

    assert response.content == "It is fine."
    assert response.tool_calls == []


def test_anthropic_sends_system_as_a_top_level_parameter() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Understood."}]})

    client = _client("anthropic", handler)
    asyncio.run(
        client.chat(
            [
                ChatMessage(role="system", content="You are NYX."),
                ChatMessage(role="system", content="Be brief."),
                ChatMessage(role="user", content="Hi"),
            ]
        )
    )

    sent = json.loads(requests[0].content)
    assert sent["system"] == "You are NYX.\nBe brief."
    assert sent["messages"] == [{"role": "user", "content": "Hi"}]
    assert all(message["role"] != "system" for message in sent["messages"])


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_no_api_key_sends_no_auth_header(protocol: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if protocol == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _client(protocol, handler)
    asyncio.run(client.chat([ChatMessage(role="user", content="Hi")]))

    assert "authorization" not in requests[0].headers
    assert "x-api-key" not in requests[0].headers


def _failure_handler(outcome: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if outcome == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("deadline exceeded", request=request)
        if outcome == "invalid_request":
            return httpx.Response(400, json={"error": "bad"})
        return httpx.Response(200, text="this is not json")

    return handler


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("connect", LLMErrorCode.UNAVAILABLE),
        ("timeout", LLMErrorCode.TIMEOUT),
        ("invalid_request", LLMErrorCode.INVALID_REQUEST),
        ("junk", LLMErrorCode.INVALID_OUTPUT),
    ],
)
def test_failures_map_to_the_right_error_code(
    protocol: str,
    outcome: str,
    expected: LLMErrorCode,
) -> None:
    client = _client(protocol, _failure_handler(outcome))

    with pytest.raises(LLMError) as excinfo:
        asyncio.run(client.chat([ChatMessage(role="user", content="Hi")]))

    assert excinfo.value.code is expected


def test_error_messages_never_contain_the_api_key() -> None:
    client = _client("openai", _failure_handler("invalid_request"), api_key=_KEY)

    with pytest.raises(LLMError) as excinfo:
        asyncio.run(client.chat([ChatMessage(role="user", content="Hi")]))

    assert _KEY not in str(excinfo.value)
    assert _KEY not in repr(excinfo.value)


def test_provider_error_messages_do_not_leak_the_key() -> None:
    """A genuine provider failure must not echo the credential.

    Credentials embedded in ``base_url`` are refused outright (see
    ``test_credentials_embedded_in_base_url_are_refused``), so this covers the remaining case: a
    valid URL whose provider call still fails.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream exploded")

    config = LLMConfig(
        protocol="openai",
        base_url="http://provider.invalid",
        api_key=_KEY,
    )
    client = OpenAICompatibleClient(config, transport=httpx.MockTransport(handler))

    with pytest.raises(LLMError) as excinfo:
        asyncio.run(client.chat([ChatMessage(role="user", content="Hi")]))

    message = str(excinfo.value)
    assert "provider.invalid" in message
    assert _KEY not in message


def test_unavailable_is_retried_up_to_max_retries() -> None:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    client = _client("openai", handler, max_retries=2)

    with pytest.raises(LLMError) as excinfo:
        asyncio.run(client.chat([ChatMessage(role="user", content="Hi")]))

    assert excinfo.value.code is LLMErrorCode.UNAVAILABLE
    assert len(calls) == 3


def test_timeout_is_not_retried_even_when_max_retries_is_set() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    client = _client("openai", handler, max_retries=3)

    with pytest.raises(LLMError) as excinfo:
        asyncio.run(client.chat([ChatMessage(role="user", content="Hi")]))

    assert excinfo.value.code is LLMErrorCode.TIMEOUT
    assert len(calls) == 1


def test_provider_responses_are_available_as_raw_without_key_material() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        payload = _openai_tool_payload()
        payload["api_key"] = _KEY
        return httpx.Response(200, json=payload)

    client = _client("openai", handler, api_key=_KEY)
    response: LLMResponse = asyncio.run(client.chat([ChatMessage(role="user", content="Hi")]))

    assert response.raw["choices"]
    assert _KEY not in response.raw
    assert "api_key" not in response.raw


def test_build_client_selects_the_protocol_backend() -> None:
    openai = build_client(LLMConfig(protocol="openai"))
    anthropic = build_client(LLMConfig(protocol="anthropic"))

    assert isinstance(openai, OpenAICompatibleClient)
    assert isinstance(anthropic, AnthropicClient)


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_credentials_embedded_in_base_url_are_refused(protocol: str) -> None:
    """A password in ``base_url`` must never reach the wire.

    ``httpx`` converts ``https://user:pass@host`` into a Basic auth header on its own, regardless of
    this client's own header logic -- so scrubbing log text is not a defence. The client refuses the
    call and points at ``LLMConfig.api_key``, which is sent as the protocol's own auth header.
    """

    config = LLMConfig(
        protocol=protocol,  # type: ignore[arg-type]
        base_url="https://alice:s3cr3t-pw@provider.example",
    )
    client = build_client(config)
    object.__setattr__(client, "_transport", httpx.MockTransport(_explode))

    with pytest.raises(LLMError) as excinfo:
        asyncio.run(client.chat([ChatMessage(role="user", content="hi")]))

    assert excinfo.value.code is LLMErrorCode.INVALID_REQUEST
    assert "api_key" in str(excinfo.value)
    # The refusal must not echo the credential either.
    assert "s3cr3t-pw" not in str(excinfo.value)


def _explode(request: httpx.Request) -> httpx.Response:
    """A transport that fails the test loudly if a refused call still reaches it."""

    raise AssertionError(
        f"a refused request reached the transport: {request.url} "
        f"authorization={request.headers.get('authorization', 'ABSENT')}"
    )


def test_a_clean_base_url_still_sends_the_configured_key() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization", "ABSENT")
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    client = build_client(LLMConfig(base_url="https://provider.example", api_key="sk-TEST-KEY"))
    object.__setattr__(client, "_transport", httpx.MockTransport(handler))
    asyncio.run(client.chat([ChatMessage(role="user", content="hi")]))

    assert seen["url"] == "https://provider.example/v1/chat/completions"
    assert seen["authorization"] == "Bearer sk-TEST-KEY"
