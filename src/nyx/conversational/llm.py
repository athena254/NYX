"""LLM provider adapter: one typed interface over two wire protocols.

The adapter only moves bytes between this process and a model provider. It decides nothing about
findings, verdicts, or dispositions. Failures are reported as :class:`LLMError` with a stable
code; provider credentials are never included in messages or payloads.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import Field

from nyx.domain.base import NyxModel

_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_MAX_TOKENS = 4096
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "x-api-key",
        "secret",
        "access_token",
        "refresh_token",
    }
)


class LLMErrorCode(StrEnum):
    """Stable failure categories for provider calls."""

    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    INVALID_OUTPUT = "invalid_output"


class LLMError(Exception):
    """A provider call failed. Carries a stable ``code``."""

    code: LLMErrorCode

    def __init__(self, message: str, *, code: LLMErrorCode = LLMErrorCode.UNAVAILABLE) -> None:
        super().__init__(message)
        self.code = code


class ToolCall(NyxModel):
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


class ChatMessage(NyxModel):
    """One message in a conversation."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class ToolSpec(NyxModel):
    """A tool the model may call, described by JSON Schema."""

    name: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    description: str
    parameters: dict[str, Any]


class LLMResponse(NyxModel):
    """A normalised model response."""

    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class LLMClient(Protocol):
    """The provider-independent chat interface."""

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse: ...


class LLMConfig(NyxModel):
    """Provider configuration; ``api_key`` is optional when a gateway holds credentials."""

    protocol: Literal["openai", "anthropic"] = "openai"
    base_url: str = "http://127.0.0.1:3000"
    api_key: str | None = None
    model: str = "deepseek-v4.1-flash-free"
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=0, ge=0)


def _as_dict(value: Any) -> dict[str, Any] | None:
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _as_list(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) else None


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _scrub(item)
            for key, item in value.items()
            if str(key).lower() not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _scrubbed_raw(payload: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _scrub(payload))


def _invalid_output(message: str) -> LLMError:
    return LLMError(message, code=LLMErrorCode.INVALID_OUTPUT)


def _optional_int(mapping: dict[str, Any], key: str) -> int | None:
    value = mapping.get(key)
    return value if isinstance(value, int) else None


def _decode_response(response: httpx.Response, url: str) -> dict[str, Any]:
    status = response.status_code
    safe_url = _safe_url(url)
    if 400 <= status < 500:
        raise LLMError(
            f"LLM provider rejected the request at {safe_url} (HTTP {status})",
            code=LLMErrorCode.INVALID_REQUEST,
        )
    if status < 200 or status >= 300:
        raise LLMError(
            f"LLM provider unavailable at {safe_url} (HTTP {status})",
            code=LLMErrorCode.UNAVAILABLE,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise _invalid_output("LLM provider returned a response that is not JSON") from exc
    decoded = _as_dict(payload)
    if decoded is None:
        raise _invalid_output("LLM provider returned an unexpected JSON payload")
    return decoded


class _HTTPClient(ABC):
    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        attempts = 0
        while True:
            try:
                return await self._chat_once(messages, tools)
            except LLMError as exc:
                if exc.code is not LLMErrorCode.UNAVAILABLE or attempts >= self._config.max_retries:
                    raise
                attempts += 1

    async def _chat_once(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
    ) -> LLMResponse:
        url = self._endpoint()
        payload = self._payload(messages, tools)
        async with httpx.AsyncClient(
            timeout=self._config.timeout_seconds,
            transport=self._transport,
        ) as client:
            try:
                response = await client.post(url, json=payload, headers=self._headers())
            except httpx.TimeoutException as exc:
                raise LLMError(
                    f"LLM provider timed out at {_safe_url(url)}",
                    code=LLMErrorCode.TIMEOUT,
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(
                    f"LLM provider unavailable at {_safe_url(url)}",
                    code=LLMErrorCode.UNAVAILABLE,
                ) from exc
        return self._parse_response(_decode_response(response, url))

    def _origin(self) -> str:
        """Return the configured base URL with any credentials removed.

        ``httpx`` turns ``https://user:pass@host`` into a Basic auth header on its own, regardless
        of this client's own header logic -- so a password embedded in ``base_url`` would go out on
        the wire even though nothing here set it, and scrubbing log text would not help. Credentials
        belong in ``LLMConfig.api_key``, which is sent as the protocol's own auth header.

        Refused rather than silently downgraded: silently dropping the password would send an
        unauthenticated request that fails in a way the user did not ask for.
        """

        raw = self._config.base_url
        parts = urlsplit(raw)
        if parts.username is None and parts.password is None:
            return raw.rstrip("/")
        raise LLMError(
            "base_url must not embed credentials; set LLMConfig.api_key instead",
            code=LLMErrorCode.INVALID_REQUEST,
        )

    @abstractmethod
    def _endpoint(self) -> str: ...

    @abstractmethod
    def _headers(self) -> dict[str, str]: ...

    @abstractmethod
    def _payload(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def _parse_response(self, payload: dict[str, Any]) -> LLMResponse: ...


def _openai_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.name is not None:
            item["name"] = message.name
        if message.tool_call_id is not None:
            item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in message.tool_calls
            ]
        wire.append(item)
    return wire


class OpenAICompatibleClient(_HTTPClient):
    """OpenAI-compatible ``/v1/chat/completions`` backend."""

    def _endpoint(self) -> str:
        return f"{self._origin()}/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    def _payload(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": _openai_messages(messages),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        return payload

    def _parse_response(self, payload: dict[str, Any]) -> LLMResponse:
        choices = _as_list(payload.get("choices"))
        if not choices:
            raise _invalid_output("LLM provider returned no choices")
        choice = _as_dict(choices[0])
        if choice is None:
            raise _invalid_output("LLM provider returned a malformed choice")
        message = _as_dict(choice.get("message"))
        if message is None:
            raise _invalid_output("LLM provider returned no message")

        raw_content = message.get("content")
        content = raw_content if isinstance(raw_content, str) else ""
        tool_calls = _openai_tool_calls(message.get("tool_calls"))
        if not content and not tool_calls:
            raise _invalid_output("LLM provider returned no usable content")

        usage = _as_dict(payload.get("usage")) or {}
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            input_tokens=_optional_int(usage, "prompt_tokens"),
            output_tokens=_optional_int(usage, "completion_tokens"),
            raw=_scrubbed_raw(payload),
        )


def _openai_tool_calls(value: Any) -> list[ToolCall]:
    if value is None:
        return []
    raw_calls = _as_list(value)
    if raw_calls is None:
        raise _invalid_output("LLM provider returned malformed tool calls")

    calls: list[ToolCall] = []
    for raw_call in raw_calls:
        call = _as_dict(raw_call)
        if call is None:
            raise _invalid_output("LLM provider returned a malformed tool call")
        call_id = call.get("id")
        function = _as_dict(call.get("function"))
        if not isinstance(call_id, str) or function is None:
            raise _invalid_output("LLM provider returned a malformed tool call")
        name = function.get("name")
        if not isinstance(name, str):
            raise _invalid_output("LLM provider returned a tool call without a name")
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except ValueError as exc:
                raise _invalid_output("LLM provider returned unparseable tool arguments") from exc
        else:
            parsed = arguments
        parsed_arguments = _as_dict(parsed)
        if parsed_arguments is None:
            raise _invalid_output("LLM provider returned tool arguments that are not an object")
        calls.append(ToolCall(id=call_id, name=name, arguments=parsed_arguments))
    return calls


def _anthropic_messages(
    messages: list[ChatMessage],
) -> tuple[str, list[dict[str, Any]]]:
    system = "\n".join(message.content for message in messages if message.role == "system")
    wire: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            wire.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id or "",
                            "content": message.content,
                        }
                    ],
                }
            )
        elif message.role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in message.tool_calls
            )
            wire.append({"role": "assistant", "content": blocks})
        else:
            wire.append({"role": message.role, "content": message.content})
    return system, wire


class AnthropicClient(_HTTPClient):
    """Anthropic Messages ``/v1/messages`` backend."""

    def _endpoint(self) -> str:
        return f"{self._origin()}/v1/messages"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        if self._config.api_key:
            headers["x-api-key"] = self._config.api_key
        return headers

    def _payload(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
    ) -> dict[str, Any]:
        system, wire_messages = _anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": _ANTHROPIC_MAX_TOKENS,
            "messages": wire_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in tools
            ]
        return payload

    def _parse_response(self, payload: dict[str, Any]) -> LLMResponse:
        raw_blocks = _as_list(payload.get("content"))
        if raw_blocks is None:
            raise _invalid_output("LLM provider returned no content blocks")

        texts: list[str] = []
        tool_calls: list[ToolCall] = []
        for raw_block in raw_blocks:
            block = _as_dict(raw_block)
            if block is None:
                raise _invalid_output("LLM provider returned a malformed content block")
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
            elif block_type == "tool_use":
                call_id = block.get("id")
                name = block.get("name")
                arguments = _as_dict(block.get("input"))
                if not isinstance(call_id, str) or not isinstance(name, str) or arguments is None:
                    raise _invalid_output("LLM provider returned a malformed tool_use block")
                tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))

        content = "".join(texts)
        if not content and not tool_calls:
            raise _invalid_output("LLM provider returned no usable content")

        usage = _as_dict(payload.get("usage")) or {}
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            input_tokens=_optional_int(usage, "input_tokens"),
            output_tokens=_optional_int(usage, "output_tokens"),
            raw=_scrubbed_raw(payload),
        )


def build_client(config: LLMConfig) -> LLMClient:
    """Build the protocol backend selected by ``config.protocol``."""

    if config.protocol == "anthropic":
        return AnthropicClient(config)
    return OpenAICompatibleClient(config)
