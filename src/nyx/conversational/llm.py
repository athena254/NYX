"""Compatibility shim: the provider moved to :mod:`nyx.llm` in Phase 6.

The conversational layer, the CLI, and the tests still import from here. Re-exporting keeps every
call site working unchanged while the implementation has one home. New code should import from
``nyx.llm``.
"""

from __future__ import annotations

from nyx.llm.provider import (
    AnthropicClient,
    ChatMessage,
    EncodedRequest,
    LLMClient,
    LLMConfig,
    LLMError,
    LLMErrorCode,
    LLMResponse,
    OpenAICompatibleClient,
    ToolCall,
    ToolSpec,
    build_client,
    decode_http_response,
    encode_request,
    parse_response,
)

__all__ = [
    "AnthropicClient",
    "ChatMessage",
    "EncodedRequest",
    "LLMClient",
    "LLMConfig",
    "LLMError",
    "LLMErrorCode",
    "LLMResponse",
    "OpenAICompatibleClient",
    "ToolCall",
    "ToolSpec",
    "build_client",
    "decode_http_response",
    "encode_request",
    "parse_response",
]
