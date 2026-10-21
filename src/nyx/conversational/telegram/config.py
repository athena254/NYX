"""Configuration for the Telegram front-end.

Deliberately narrow: a token, an allowlist of who may talk to the bot, and a per-user rate
limit. Everything else is a default chosen to fail closed rather than to be convenient.

The dataclass carries **plain values only** -- no ``Settings``, no environment reader. The CLI
is what turns NYX settings into one of these, which keeps the conversational package free of
any dependency on the deterministic core's configuration module.
"""

from __future__ import annotations

from dataclasses import dataclass

#: What a bot reply looks like when the per-user budget is spent.
RATE_LIMITED = "You have sent a lot of messages. Please wait a minute and try again."

#: What a bot reply looks like when something unexpected fails. It deliberately carries no
#: internals: a Telegram chat is not a place to publish a traceback or a configuration detail.
INTERNAL_ERROR = (
    "Something went wrong while answering that. Nothing was changed in NYX -- "
    "ask again, or check the server log."
)


@dataclass(frozen=True)
class TelegramConfig:
    """Everything the bot needs to run, with no ambient state."""

    bot_token: str
    api_base: str
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    require_private_chat: bool
    max_message_length: int
    timeout_seconds: float
    poll_timeout_seconds: int
    rate_limit_requests: int
    rate_limit_window_seconds: float
    actor_prefix: str

    def actor_for(self, user_id: int) -> str:
        """The identity NYX's audit trail records for a message from ``user_id``.

        The caller's identity is threaded through rather than defaulted. A bot has no identity
        of its own -- the only real principal is the Telegram account it is talking to -- so the
        recorded actor is derived from that account and never invented.
        """

        return f"{self.actor_prefix}{user_id}"

    @property
    def is_configured(self) -> bool:
        """Whether the bot has the minimum it needs to talk to Telegram at all."""

        return bool(self.bot_token) and bool(self.allowed_user_ids)
