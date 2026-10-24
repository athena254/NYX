"""The provider seam shared by the conversational layer and the model critic (Phase 6).

``nyx.conversational.llm`` used to own this code. It is provider transport and nothing else: it
decides no finding, no verdict, and no disposition. Keeping it here lets the synchronous critic
transport and the asynchronous chat client share one wire implementation.

See :mod:`nyx.llm.provider` for the full contract.
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
