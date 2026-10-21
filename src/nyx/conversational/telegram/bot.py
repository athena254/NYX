"""The Telegram bot: poll, admit, answer, and never leak.

The whole of this module is one rule applied three times:

1. **Admission first.** A message from a non-private chat, a non-allowlisted user, or one
   whose chat id does not match its sender is dropped before any model call and before any
   reply.
2. **Replies go only where the admitted message came from.** A reply is sent to the chat id
   of the message being answered -- never to an id found in the message text.
3. **Nothing internal is ever sent.** A failure produces a fixed, generic sentence. Details
   go to the server log, which is the only place a traceback belongs.

The agent it drives is the same :class:`~nyx.conversational.agent.ChatAgent` that backs
``nyx chat``, so the safety property is inherited rather than restated: the model can query
NYX and explain what it finds, and there is no tool by which it could create a finding,
record a disposition, or change a lifecycle state.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from nyx.conversational.agent import ChatAgent, ChatError
from nyx.conversational.telegram.adapter import TelegramAdapter, TelegramError
from nyx.conversational.telegram.admission import admit
from nyx.conversational.telegram.config import (
    INTERNAL_ERROR,
    RATE_LIMITED,
    TelegramConfig,
)
from nyx.conversational.telegram.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

#: The bot's command menu. These are answered without a model call, so they cost nothing and
#: are never rate limited -- a user who spams /start has spent no money and changed nothing.
COMMANDS: tuple[dict[str, str], ...] = (
    {"command": "start", "description": "What this bot is, and what it can and cannot do"},
    {"command": "help", "description": "How to ask NYX about its record"},
)

START_REPLY = (
    "I am the conversational interface to NYX, an evidence-backed critic.\n\n"
    "Ask me what NYX has recorded and I will answer from its record, citing the ids you can "
    "check yourself. I can file a rebuttal against a finding and request verification.\n\n"
    "I cannot create a finding, record a disposition, or change a lifecycle state. Those "
    "powers do not exist in my tool surface."
)

HELP_REPLY = (
    "Ask in plain language, for example:\n\n"
    "- What findings does NYX have?\n"
    "- Show me finding <id>\n"
    "- What is the dispute history for finding <id>?\n"
    "- Challenge finding <id>: <your argument>\n\n"
    "Every answer cites the NYX ids it came from. I only report what NYX recorded -- "
    "I do not judge, and I never invent a verdict."
)

#: How many chat sessions to keep. A bot that remembered every chat forever would grow without
#: bound; evicting the least recently used chat only costs that user their conversation history.
MAX_TRACKED_CHATS = 64

#: How many update ids to remember. Telegram can redeliver an update, and a redelivered update
#: that filed a rebuttal would file it twice -- so the id is checked before anything happens.
MAX_TRACKED_UPDATES = 1000

#: How long to wait before retrying a failed long-poll. Without it, a persistent failure (a
#: revoked token, a network partition) turns into a hot loop that hammers the Bot API as fast
#: as the loop can turn. A poller that cannot reach Telegram should be patient, not busy.
POLL_RETRY_SECONDS = 5.0


class TelegramBot:
    """Polls Telegram, admits messages, and answers them through a chat agent."""

    def __init__(
        self,
        config: TelegramConfig,
        *,
        agent_factory: Callable[[str], ChatAgent],
        adapter: TelegramAdapter | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._config = config
        self._agent_factory = agent_factory
        self._adapter = adapter if adapter is not None else TelegramAdapter(config)
        self._limiter = (
            limiter
            if limiter is not None
            else RateLimiter(
                config.rate_limit_requests,
                config.rate_limit_window_seconds,
            )
        )
        self._agents: OrderedDict[int, ChatAgent] = OrderedDict()
        self._seen: OrderedDict[int, None] = OrderedDict()

    # -- introspection -----------------------------------------------------

    @property
    def adapter(self) -> TelegramAdapter:
        return self._adapter

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    def describe(self) -> dict[str, Any]:
        """A secret-safe summary of the bot's configuration, for ``nyx telegram check``."""

        return {
            "api_base": self._config.api_base,
            "allowed_user_count": len(self._config.allowed_user_ids),
            "allowed_chat_count": len(self._config.allowed_chat_ids),
            "require_private_chat": self._config.require_private_chat,
            "max_message_length": self._config.max_message_length,
            "poll_timeout_seconds": self._config.poll_timeout_seconds,
            "rate_limit_requests": self._config.rate_limit_requests,
            "rate_limit_window_seconds": self._config.rate_limit_window_seconds,
            "token_configured": bool(self._config.bot_token),
            "commands": [entry["command"] for entry in COMMANDS],
        }

    async def register_commands(self) -> bool:
        """Publish the command menu. A failure here is logged, never raised into the poller."""

        try:
            return await self._adapter.set_my_commands([dict(entry) for entry in COMMANDS])
        except TelegramError:
            logger.exception("could not register the Telegram command menu")
            return False

    # -- one update --------------------------------------------------------

    async def handle_update(self, update: Any) -> dict[str, Any]:
        """Handle one Telegram update. Never raises: a bad update is a result, not a crash."""

        if not isinstance(update, dict):
            return {"status": "ignored", "reason": "malformed update"}

        update_id = update.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            return {"status": "ignored", "reason": "malformed update id"}

        # Duplicate protection comes before admission: a redelivered update must not be able
        # to file a second rebuttal, and it must not even reach the rate limiter.
        if not self._mark_seen(update_id):
            return {"status": "duplicate", "update_id": update_id}

        message = update.get("message")
        if not isinstance(message, dict):
            return {"status": "ignored", "reason": "no message"}

        verdict = admit(message, config=self._config)
        if not verdict.allowed:
            logger.info(
                "refused a Telegram message",
                extra={"reason": verdict.reason},
            )
            return {"status": "rejected", "reason": verdict.reason, "update_id": update_id}

        assert verdict.chat_id is not None and verdict.user_id is not None  # nosec B101
        text = (verdict.text or "").strip()

        if text.startswith("/"):
            reply = self._command(text)
            await self._reply(verdict.chat_id, reply)
            return {"status": "ok", "kind": "command", "update_id": update_id}

        # The limiter is consulted only now, so a user who is active is the one who pays for
        # it, and a deterministic command never consumes model budget.
        if not self._limiter.allow(verdict.user_id):
            await self._reply(verdict.chat_id, RATE_LIMITED)
            return {"status": "rejected", "reason": "rate_limited", "update_id": update_id}

        reply = await self._answer(text, verdict.chat_id)
        await self._reply(verdict.chat_id, reply)
        return {"status": "ok", "kind": "answer", "update_id": update_id}

    def _command(self, text: str) -> str:
        """Answer a slash command without spending a model call."""

        name = text.strip().lower().split(None, 1)[0]
        if name in {"/start", "/help"}:
            return START_REPLY if name == "/start" else HELP_REPLY
        return f"Unknown command {name}. Try /help."

    async def _answer(self, text: str, chat_id: int) -> str:
        """Ask the agent, turning every failure into a fixed, non-internal sentence."""

        agent = self._agent_for(chat_id)
        try:
            return await agent.run_async(text)
        except ChatError as exc:
            # A ChatError message is written to be shown to a human; it names a missing
            # variable or an unavailable model, never a stack frame.
            return f"I could not answer that: {exc}"
        except Exception:
            # The catch-all is deliberate. A Telegram chat must never receive a traceback,
            # a file path, or a configuration value, so the detail goes to the log only.
            logger.exception("the chat agent failed for chat %s", chat_id)
            return INTERNAL_ERROR

    def _agent_for(self, chat_id: int) -> ChatAgent:
        """Return the agent for one chat, creating it on first use.

        Each chat keeps its own conversation, so a follow-up question has the earlier turns in
        context. The table is bounded and evicts the least recently used chat, because a bot
        with no bound grows forever and a lost conversation history costs nothing.
        """

        agent = self._agents.get(chat_id)
        if agent is not None:
            self._agents.move_to_end(chat_id)
            return agent
        agent = self._agent_factory(self._config.actor_for(chat_id))
        self._agents[chat_id] = agent
        self._agents.move_to_end(chat_id)
        while len(self._agents) > MAX_TRACKED_CHATS:
            self._agents.popitem(last=False)
        return agent

    def _mark_seen(self, update_id: int) -> bool:
        """Record an update id, returning ``False`` if it was already handled."""

        if update_id in self._seen:
            return False
        self._seen[update_id] = None
        while len(self._seen) > MAX_TRACKED_UPDATES:
            self._seen.popitem(last=False)
        return True

    async def _reply(self, chat_id: int, text: str) -> None:
        """Send a reply, logging a failure instead of raising it into the poller."""

        try:
            await self._adapter.send_message(chat_id, text)
        except TelegramError:
            # One chat failing must not stop the bot serving every other chat.
            logger.exception("could not send a Telegram reply to chat %s", chat_id)

    # -- the poller --------------------------------------------------------

    async def run(self, *, register_commands: bool = True) -> None:
        """Long-poll until the process is stopped.

        The offset advances *before* an update is handled, so an update that fails to process
        is not retried forever. Combined with the seen-id check, a redelivered update is
        dropped rather than replayed.
        """

        if register_commands:
            await self.register_commands()
        logger.info("Telegram bot polling started")
        offset: int | None = None
        while True:
            try:
                updates = await self._adapter.get_updates(offset=offset)
            except TelegramError:
                # A throttled or failed long-poll is not fatal. Back off before asking again:
                # a token that was revoked will still be revoked in a millisecond, and a hot
                # retry loop would only make the failure louder.
                logger.warning(
                    "Telegram getUpdates failed; retrying in %ss",
                    POLL_RETRY_SECONDS,
                )
                await asyncio.sleep(POLL_RETRY_SECONDS)
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int) and not isinstance(update_id, bool):
                    offset = update_id + 1
                try:
                    await self.handle_update(update)
                except Exception:
                    # Nothing a single update can do may stop the bot.
                    logger.exception("unhandled error while processing a Telegram update")

    async def close(self) -> None:
        await self._adapter.close()
