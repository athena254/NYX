"""Synchronous provider transport for the model critic (Phase 6).

:class:`~nyx.critics.llm.LlmCritic` is synchronous: it is driven by the critic registry, the
watcher, and the request handlers, none of which offers an event loop. The chat client in
:mod:`nyx.llm.provider` is asynchronous because the conversational agent is. Rather than run an
event loop inside a request, this module implements the small transport seam with
``httpx.Client`` and shares the wire implementation with the async client through
:func:`~nyx.llm.provider.encode_request` and :func:`~nyx.llm.provider.parse_response`.

Two properties this module exists to make true:

* **Failure maps to the critic's own error types.** The critic catches
  :class:`~nyx.critics.llm.LlmTimeoutError` and
  :class:`~nyx.critics.llm.LlmTransportError`; a provider error must not escape as a raw
  ``httpx`` exception, which the critic would still turn into ``ERROR`` but with a less useful
  rationale.
* **Secrets never reach a message.** The provider URL is reduced to scheme/host/path before it
  appears in an error, and credentials embedded in ``base_url`` are refused by the shared encoder.

The transport decides nothing. It returns text; the critic validates it.
"""

from __future__ import annotations

from typing import Any

import httpx

from nyx.critics.llm import (
    OUTPUT_SCHEMA,
    LlmCritic,
    LlmInvalidOutputError,
    LlmTimeoutError,
    LlmTransportError,
)
from nyx.llm.provider import (
    ChatMessage,
    LLMConfig,
    LLMError,
    LLMErrorCode,
    decode_http_response,
    encode_request,
    parse_response,
    safe_url,
)

_DEADLINE_NOTE = "No finding was composed and no stored state was changed."


class SyncCriticTransport:
    """An :class:`~nyx.critics.llm.LlmTransport` over ``httpx.Client``.

    ``last_usage`` holds the token counts from the most recent successful call (or ``None``),
    so the caller can record cost without the transport deciding what to do with it.
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self.last_usage: dict[str, int | None] | None = None

    def complete(self, *, prompt: str, schema: dict[str, Any], timeout_seconds: float) -> str:
        """Return the model's raw text for ``prompt``, or raise a critic transport error.

        ``schema`` is not sent to the provider: the critic already embeds the JSON Schema in the
        prompt and validates the response itself. It stays in the signature because it is the
        interface every transport implements.
        """

        try:
            request = encode_request(
                self._config,
                [ChatMessage(role="user", content=prompt)],
            )
        except LLMError as exc:
            raise _transport_error(exc) from exc

        deadline = min(timeout_seconds, self._config.timeout_seconds)
        try:
            with httpx.Client(timeout=deadline, transport=self._transport) as client:
                response = client.post(request.url, json=request.payload, headers=request.headers)
        except httpx.TimeoutException as exc:
            raise LlmTimeoutError(
                f"The LLM provider did not respond before the deadline at "
                f"{safe_url(request.url)}. {_DEADLINE_NOTE}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LlmTransportError(
                f"The LLM provider could not be reached at {safe_url(request.url)}. "
                f"{_DEADLINE_NOTE}"
            ) from exc

        try:
            parsed = parse_response(self._config, decode_http_response(response, request.url))
        except LLMError as exc:
            raise _transport_error(exc) from exc

        if not parsed.content.strip():
            raise LlmInvalidOutputError(
                "The LLM provider returned no text content. " + _DEADLINE_NOTE
            )

        self.last_usage = {
            "input_tokens": parsed.input_tokens,
            "output_tokens": parsed.output_tokens,
        }
        return parsed.content


def build_llm_critic(
    config: LLMConfig,
    *,
    provider: str | None = None,
    log_prompts: bool = False,
    timeout_seconds: float | None = None,
) -> LlmCritic:
    """Build the model critic wired to ``config``.

    ``provider`` is the human-facing provider name when the deployment names one; otherwise the
    wire protocol is recorded. ``model_version`` is the model id the provider was asked for, not
    a claim about which weights actually answered.
    """

    return LlmCritic(
        transport=SyncCriticTransport(config),
        schema=OUTPUT_SCHEMA,
        timeout_seconds=config.timeout_seconds if timeout_seconds is None else timeout_seconds,
        model_provider=provider or config.protocol,
        model_version=config.model,
        log_prompts=log_prompts,
    )


def _transport_error(exc: LLMError) -> LlmTransportError:
    """Map a provider error onto the critic's transport hierarchy."""

    if exc.code is LLMErrorCode.TIMEOUT:
        return LlmTimeoutError(str(exc))
    if exc.code is LLMErrorCode.INVALID_OUTPUT:
        return LlmInvalidOutputError(str(exc))
    return LlmTransportError(str(exc))


__all__ = ["SyncCriticTransport", "build_llm_critic"]
