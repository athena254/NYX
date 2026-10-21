"""Admission: who may talk to the bot, and what they may send.

This is the authorisation boundary. It is separate from the rate limiter because the two
answer different questions -- *may this person use the bot at all* versus *how fast* -- and
conflating them is how a bot ends up serving strangers.

Every check fails closed. A field that is missing, the wrong type, or a bool where an int was
expected is a refusal, not a default: a bool is an ``int`` in Python, so ``True`` would
otherwise be admitted as user ``1``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nyx.conversational.telegram.config import TelegramConfig


@dataclass(frozen=True)
class Admission:
    """The verdict on one inbound Telegram message."""

    allowed: bool
    user_id: int | None = None
    chat_id: int | None = None
    text: str | None = None
    reason: str | None = None

    @classmethod
    def refused(cls, reason: str) -> Admission:
        return cls(allowed=False, reason=reason)


def _as_id(value: Any) -> int | None:
    """Return ``value`` as a positive Telegram id, or ``None`` if it is not one.

    ``bool`` is rejected explicitly: it is a subclass of ``int``, so without this check
    ``True`` would be admitted as user ``1``.
    """

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def admit(message: Any, *, config: TelegramConfig) -> Admission:
    """Decide whether one Telegram message may reach the model.

    The order matters: identity is checked before chat type, so a refusal never reveals
    whether a chat id was on the allowlist.
    """

    if not isinstance(message, dict):
        return Admission.refused("no message")

    sender = message.get("from")
    sender = sender if isinstance(sender, dict) else {}
    user_id = _as_id(sender.get("id"))
    if user_id is None:
        return Admission.refused("missing Telegram user id")

    if user_id not in config.allowed_user_ids:
        return Admission.refused("user not invited")

    chat = message.get("chat")
    chat = chat if isinstance(chat, dict) else {}
    chat_id = _as_id(chat.get("id"))
    if chat_id is None:
        return Admission.refused("missing Telegram chat id")

    if config.require_private_chat:
        if chat.get("type") != "private":
            return Admission.refused("private chat required")
        # In a private chat the chat id *is* the user id. A mismatch means the payload is
        # inconsistent, and answering would risk replying into a chat the sender does not own.
        if chat_id != user_id:
            return Admission.refused("chat id mismatch")

    if config.allowed_chat_ids and chat_id not in config.allowed_chat_ids:
        return Admission.refused("chat not invited")

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return Admission.refused("missing text")
    if len(text) > config.max_message_length:
        return Admission.refused("message too long")

    return Admission(allowed=True, user_id=user_id, chat_id=chat_id, text=text)
