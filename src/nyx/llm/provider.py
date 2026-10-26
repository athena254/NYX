"""LLM provider adapter: one typed interface over two wire protocols.

The adapter only moves bytes between this process and a model provider. It decides nothing about
findings, verdicts, or dispositions. Failures are reported as :class:`LLMError` with a stable
code; provider credentials are never included in messages or payloads.

**Ownership.** This module was ``nyx.conversational.llm`` until Phase 6. The conversational layer
is only one consumer; the model critic is the other, and it is synchronous. Keeping the provider
under ``nyx.llm`` lets both call styles share one encode/decode implementation instead of drifting
apart. ``nyx.conversational.llm`` remains as a re-export shim for existing call sites.

**Two call styles, one wire shape.** :func:`encode_request` and :func:`parse_response` are the
single source of truth for the wire protocol. The asynchronous clients in this module and the
synchronous critic transport (``nyx.critics.llm_transport``) both call them, and a contract test
asserts the two styles send identical HTTP requests.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class EncodedRequest:
    """Everything needed to issue one provider request, protocol-independent."""

    url: str
    headers: dict[str, str]
    payload: dict[str, Any]


def _as_dict(value: Any) -> dict[str, Any] | None:
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _as_list(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) else None


def safe_url(url: str) -> str:
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


def decode_http_response(response: httpx.Response, url: str) -> dict[str, Any]:
    status = response.status_code
    shown_url = safe_url(url)
    if 400 <= status < 500:
        raise LLMError(
            f"LLM provider rejected the request at {shown_url} (HTTP {status})",
            code=LLMErrorCode.INVALID_REQUEST,
        )
    if status < 200 or status >= 300:
        raise LLMError(
            f"LLM provider unavailable at {shown_url} (HTTP {status})",
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


def _origin(config: LLMConfig) -> str:
    """Return the configured base URL with any credentials removed.

    ``httpx`` turns ``https://user:pass@host`` into a Basic auth header on its own, regardless of
    this client's own header logic -- so a password embedded in ``base_url`` would go out on the
    wire even though nothing here set it, and scrubbing log text would not help. Credentials
    belong in ``LLMConfig.api_key``, which is sent as the protocol's own auth header.

    Refused rather than silently downgraded: silently dropping the password would send an
    unauthenticated request that fails in a way the user did not ask for.
    """

    raw = config.base_url
    parts = urlsplit(raw)
    if parts.username is None and parts.password is None:
        return raw.rstrip("/")
    raise LLMError(
        "base_url must not embed credentials; set LLMConfig.api_key instead",
        code=LLMErrorCode.INVALID_REQUEST,
    )


_FENCED_DOCUMENT = re.compile(r"^\s*```[A-Za-z0-9_-]*[ \t]*\r?\n(.*)\r?\n?```[ \t]*$", re.DOTALL)


def _first_json_span(text: str) -> str | None:
    """Return the first balanced ``{...}`` or ``[...]`` span in ``text``, or ``None``.

    String literals are honoured, so a brace inside a quoted value does not open or close the
    span and an escaped quote does not end one. A span that never closes is not a span.
    """

    opener_at = -1
    opener = ""
    for index, char in enumerate(text):
        if char in "{[":
            opener_at, opener = index, char
            break
    if opener_at < 0:
        return None

    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(opener_at, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[opener_at : index + 1]
    return None


def extract_json_document(text: str) -> str:
    """Return the JSON document inside ``text``, with any markdown wrapper removed.

    Providers routinely wrap a JSON answer in a ```` ```json ```` fence even when the prompt asked
    for JSON only. That is a wrapper, not a refusal: the model answered, and rejecting it as "not
    valid JSON" throws a correct answer away. The failure then presents as a model-quality
    problem when it is a plumbing one, which is how a live run came to report
    ``"model": "error"`` on a case the model had actually answered.

    This removes the wrapper and **nothing else**. It does not repair malformed JSON and it does
    not widen what is accepted: the caller still parses the result and still validates it against
    its own schema, so a fenced document that violates the schema is rejected exactly as an
    unfenced one would be.

    Raises ``ValueError`` when no JSON span is present rather than returning the input unchanged:
    a pass-through would move a "no JSON here" failure one layer later into a
    ``json.JSONDecodeError`` with a worse message, and the caller already converts ``ValueError``
    into its own typed failure.
    """

    stripped = text.strip()
    fenced = _FENCED_DOCUMENT.match(stripped)
    if fenced is not None:
        stripped = fenced.group(1).strip()
    span = _first_json_span(stripped)
    if span is None:
        raise ValueError("the response contains no JSON document")
    return span


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


def encode_request(
    config: LLMConfig,
    messages: list[ChatMessage],
    tools: list[ToolSpec] | None = None,
) -> EncodedRequest:
    """Build the wire request for ``config.protocol``.

    The single implementation both call styles use. Raises :class:`LLMError` with
    ``INVALID_REQUEST`` if ``base_url`` embeds credentials.
    """

    origin = _origin(config)
    if config.protocol == "anthropic":
        system, wire_messages = _anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": config.model,
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
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        if config.api_key:
            headers["x-api-key"] = config.api_key
        return EncodedRequest(url=f"{origin}/v1/messages", headers=headers, payload=payload)

    payload = {
        "model": config.model,
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
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return EncodedRequest(
        url=f"{origin}/v1/chat/completions",
        headers=headers,
        payload=payload,
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


def _parse_openai_response(payload: dict[str, Any]) -> LLMResponse:
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


def _parse_anthropic_response(payload: dict[str, Any]) -> LLMResponse:
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


def parse_response(config: LLMConfig, payload: dict[str, Any]) -> LLMResponse:
    """Normalise a decoded provider payload for ``config.protocol``."""

    if config.protocol == "anthropic":
        return _parse_anthropic_response(payload)
    return _parse_openai_response(payload)


class _HTTPClient:
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
        request = encode_request(self._config, messages, tools)
        async with httpx.AsyncClient(
            timeout=self._config.timeout_seconds,
            transport=self._transport,
        ) as client:
            try:
                response = await client.post(
                    request.url, json=request.payload, headers=request.headers
                )
            except httpx.TimeoutException as exc:
                raise LLMError(
                    f"LLM provider timed out at {safe_url(request.url)}",
                    code=LLMErrorCode.TIMEOUT,
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(
                    f"LLM provider unavailable at {safe_url(request.url)}",
                    code=LLMErrorCode.UNAVAILABLE,
                ) from exc
        return parse_response(self._config, decode_http_response(response, request.url))


class OpenAICompatibleClient(_HTTPClient):
    """OpenAI-compatible ``/v1/chat/completions`` backend."""


class AnthropicClient(_HTTPClient):
    """Anthropic Messages ``/v1/messages`` backend."""


def build_client(config: LLMConfig) -> LLMClient:
    """Build the protocol backend selected by ``config.protocol``."""

    if config.protocol == "anthropic":
        return AnthropicClient(config)
    return OpenAICompatibleClient(config)
