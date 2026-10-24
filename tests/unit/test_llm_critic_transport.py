"""The synchronous critic transport speaks the same protocols and fails closed.

Nothing here touches a real provider: ``httpx.MockTransport`` stands in for the socket.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nyx.critics.llm import (
    OUTPUT_SCHEMA,
    LlmInvalidOutputError,
    LlmTimeoutError,
    LlmTransportError,
)
from nyx.critics.llm_transport import SyncCriticTransport
from nyx.llm.provider import LLMConfig

_KEY = "sk-critic-distinctive-key-7b21"
Handler = Callable[[httpx.Request], httpx.Response]

_OPENAI_OK = {
    "choices": [{"message": {"role": "assistant", "content": '{"result": "no_finding"}'}}],
    "usage": {"prompt_tokens": 41, "completion_tokens": 7},
}
_ANTHROPIC_OK = {
    "content": [{"type": "text", "text": '{"result": "no_finding"}'}],
    "usage": {"input_tokens": 41, "output_tokens": 7},
}


def _config(protocol: str = "openai", **overrides: Any) -> LLMConfig:
    values: dict[str, Any] = {
        "protocol": protocol,
        "base_url": "http://127.0.0.1:3000",
        "api_key": _KEY,
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return LLMConfig(**values)


def _run(handler: Handler, config: LLMConfig | None = None) -> tuple[str, SyncCriticTransport]:
    transport = SyncCriticTransport(
        config or _config(),
        transport=httpx.MockTransport(handler),
    )
    return (
        transport.complete(prompt="Assess this.", schema=OUTPUT_SCHEMA, timeout_seconds=30.0),
        transport,
    )


def test_openai_response_returns_content_and_records_usage() -> None:
    content, transport = _run(lambda _request: httpx.Response(200, json=_OPENAI_OK))

    assert content == '{"result": "no_finding"}'
    assert transport.last_usage == {"input_tokens": 41, "output_tokens": 7}


def test_anthropic_response_returns_content_and_records_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/v1/messages")
        assert request.headers["x-api-key"] == _KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(200, json=_ANTHROPIC_OK)

    content, transport = _run(handler, _config("anthropic"))

    assert content == '{"result": "no_finding"}'
    assert transport.last_usage == {"input_tokens": 41, "output_tokens": 7}


def test_timeout_maps_to_the_critic_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("deadline exceeded", request=request)

    with pytest.raises(LlmTimeoutError):
        _run(handler)


def test_connection_failure_maps_to_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LlmTransportError):
        _run(handler)


@pytest.mark.parametrize("status", [400, 401, 429, 500, 502, 503])
def test_http_failures_map_to_a_transport_error(status: int) -> None:
    with pytest.raises(LlmTransportError):
        _run(lambda _request: httpx.Response(status, text="upstream failure"))


def test_non_json_body_is_an_invalid_output_error() -> None:
    with pytest.raises(LlmInvalidOutputError):
        _run(lambda _request: httpx.Response(200, text="this is not json"))


def test_empty_content_is_an_invalid_output_error() -> None:
    payload = {"choices": [{"message": {"role": "assistant", "content": "   "}}]}

    with pytest.raises(LlmInvalidOutputError):
        _run(lambda _request: httpx.Response(200, json=payload))


def test_credentials_in_base_url_are_refused_before_the_wire() -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a refused request reached the transport")

    config = _config(
        base_url="https://alice:s3cr3t-pw@provider.example",
    )
    with pytest.raises(LlmTransportError) as excinfo:
        _run(explode, config)

    message = str(excinfo.value)
    assert "s3cr3t-pw" not in message
    assert "api_key" in message


def test_errors_never_contain_the_api_key() -> None:
    with pytest.raises(LlmTransportError) as excinfo:
        _run(lambda _request: httpx.Response(502, text="upstream exploded"))

    assert _KEY not in str(excinfo.value)
