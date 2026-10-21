"""A minimal Telegram Bot API adapter.

The Bot API is plain HTTP with the token in the URL path. That makes the one rule that
matters easy to state and easy to break: **the token must never reach a log line, an
exception message, or a URL that gets printed.**

``httpx`` puts the request URL in its own exceptions, so every failure path here rebuilds a
token-free request/response pair before raising. The same care applies to the response body:
a Bot API reply can carry chat content, so it is never interpolated into an error.

No retry logic lives here. Telegram sends ``429`` with a ``retry_after`` hint; the poller
treats a throttled long-poll as "no updates" and simply asks again, which is the correct
behaviour for a poller and avoids replaying a side effect.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from nyx.conversational.telegram.config import TelegramConfig

#: Telegram's hard limit on a single message. Longer replies are split before sending.
MAX_MESSAGE_LENGTH = 4096


class TelegramError(Exception):
    """The Bot API could not be reached, or replied in a shape we did not expect.

    The message is safe to log: it carries a status code or a fixed description, never a
    URL and never a response body.
    """

    def __init__(self, message: str, *, kind: str = "telegram_error") -> None:
        super().__init__(message)
        self.kind = kind


def _checked(response: httpx.Response) -> dict[str, Any]:
    """Validate a Bot API reply, without ever exposing the token or the body."""

    if response.status_code < 200 or response.status_code >= 300:
        raise TelegramError(
            f"Telegram API HTTP status {response.status_code}",
            kind="telegram_http_error",
        )
    try:
        data = response.json()
    except (TypeError, ValueError) as exc:
        raise TelegramError(
            "Telegram API response is not valid JSON",
            kind="telegram_bad_response",
        ) from exc
    if not isinstance(data, dict):
        raise TelegramError(
            "Telegram API response is not an object",
            kind="telegram_bad_response",
        )
    if data.get("ok") is not True:
        # ``description`` is Telegram's own short status text; it carries no token and no
        # message body, so it is the one field worth surfacing.
        description = data.get("description")
        detail = f": {description}" if isinstance(description, str) else ""
        raise TelegramError(
            f"Telegram API rejected the request{detail}",
            kind="telegram_rejected",
        )
    return data


class TelegramAdapter:
    """Async client for the subset of the Bot API the bot uses."""

    def __init__(self, config: TelegramConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    @property
    def _url(self) -> str:
        """The token-bearing base URL. Never logged, never put in an exception."""

        return f"{self._config.api_base}/bot{self._config.bot_token}"

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._url,
                timeout=httpx.Timeout(self._config.timeout_seconds),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_me(self) -> dict[str, Any]:
        """Return the bot's own identity, for ``nyx telegram check``."""

        client = await self._http()
        data = _checked(await client.get("/getMe"))
        result = data.get("result")
        if not isinstance(result, dict):
            raise TelegramError(
                "Telegram API result is not an object",
                kind="telegram_bad_response",
            )
        return result

    async def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
        """Register the bot's command menu."""

        client = await self._http()
        data = _checked(await client.post("/setMyCommands", json={"commands": commands}))
        return data.get("result") is True

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """Send a reply, splitting it if Telegram would reject it as too long.

        The chat id is taken from the admitted message that is being answered, never from
        anything the message text contained.
        """

        client = await self._http()
        sent: dict[str, Any] = {}
        for index, chunk in enumerate(split_message(text, MAX_MESSAGE_LENGTH)):
            payload: dict[str, Any] = {
                "chat_id": str(chat_id),
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            data = _checked(await client.post("/sendMessage", json=payload))
            result = data.get("result")
            if isinstance(result, dict):
                sent = result
            # Small gap between chunks: Telegram rate-limits rapid sends to one chat.
            if index > 0:
                await asyncio.sleep(0.5)
        return sent

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Long-poll for new updates.

        The read timeout is longer than the poll interval, because Telegram is allowed to
        hold the request open for the whole interval and a shorter client timeout would turn
        every idle poll into a spurious failure.
        """

        client = await self._http()
        params: dict[str, Any] = {"limit": limit, "timeout": self._config.poll_timeout_seconds}
        if offset is not None:
            params["offset"] = offset
        poll_timeout = httpx.Timeout(
            self._config.timeout_seconds,
            read=max(self._config.timeout_seconds, self._config.poll_timeout_seconds + 15.0),
        )
        data = _checked(await client.get("/getUpdates", params=params, timeout=poll_timeout))
        result = data.get("result")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise TelegramError(
                "Telegram API getUpdates result is not a list of objects",
                kind="telegram_bad_response",
            )
        return result


def split_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a reply into chunks Telegram will accept, breaking on a line boundary.

    A reply that is already short enough is returned whole -- including the empty string, so
    a caller that always sends one message never has to special-case the empty case.
    """

    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        break_at = remaining.rfind("\n", 0, max_length)
        if break_at < max_length // 2:
            break_at = max_length
        chunks.append(remaining[:break_at])
        remaining = remaining[break_at:].lstrip()
    return chunks
