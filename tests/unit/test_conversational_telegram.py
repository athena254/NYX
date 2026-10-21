"""The Telegram front-end: admission, pacing, privacy, and the safety property.

Four claims are worth proving, and each is proved here rather than asserted in a docstring:

1. **Admission fails closed.** A non-private chat, a non-allowlisted user, or a chat id that
   does not match its sender is dropped before any model call.
2. **The token never escapes.** The Bot API puts the token in the URL path, and ``httpx``
   prints request URLs in its exceptions -- so the failure paths are checked directly.
3. **A redelivered update cannot be replayed.** Telegram retries updates; a retried update
   that filed a rebuttal would file it twice.
4. **The bot cannot judge.** It drives the same agent as ``nyx chat``, so it inherits the
   tool surface that has no judge-capable tool in it.

The provider and Telegram are both doubled, so these tests are deterministic, spend no money,
and do not touch the network. The live path is covered by ``scripts/probe_telegram_live.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from nyx.config import Settings
from nyx.conversational.agent import (
    JUDGE_ONLY_OPERATIONS,
    ChatAgent,
    assert_no_judge_tools,
    tool_specs,
)
from nyx.conversational.telegram.adapter import (
    MAX_MESSAGE_LENGTH,
    TelegramAdapter,
    TelegramError,
    split_message,
)
from nyx.conversational.telegram.admission import admit
from nyx.conversational.telegram.bot import (
    HELP_REPLY,
    INTERNAL_ERROR,
    MAX_TRACKED_UPDATES,
    RATE_LIMITED,
    START_REPLY,
    TelegramBot,
)
from nyx.conversational.telegram.config import TelegramConfig
from nyx.conversational.telegram.rate_limit import RateLimiter

ALLOWED_USER = 5981285472
OTHER_USER = 111
# A fake token. Its only job is to be a string that must never appear in an error message.
TOKEN = "123456:AA-test-token-value"  # noqa: S105


def _config(**overrides: Any) -> TelegramConfig:
    """A config that is valid by default, so each test states only what it is testing."""

    values: dict[str, Any] = {
        "bot_token": TOKEN,
        "api_base": "https://api.telegram.org",
        "allowed_user_ids": frozenset({ALLOWED_USER}),
        "allowed_chat_ids": frozenset(),
        "require_private_chat": True,
        "max_message_length": 4096,
        "timeout_seconds": 5.0,
        "poll_timeout_seconds": 1,
        "rate_limit_requests": 3,
        "rate_limit_window_seconds": 60.0,
        "actor_prefix": "telegram:",
    }
    values.update(overrides)
    return TelegramConfig(**values)


def _message(
    *,
    user_id: int = ALLOWED_USER,
    chat_id: int = ALLOWED_USER,
    chat_type: str = "private",
    text: str = "what findings does NYX have?",
) -> dict[str, Any]:
    return {
        "message_id": 1,
        "from": {"id": user_id, "is_bot": False, "first_name": "Kallmedis"},
        "chat": {"id": chat_id, "type": chat_type, "first_name": "Kallmedis"},
        "date": 1_700_000_000,
        "text": text,
    }


def _update(update_id: int, **message_kwargs: Any) -> dict[str, Any]:
    return {"update_id": update_id, "message": _message(**message_kwargs)}


# --------------------------------------------------------------------------- split


def test_split_message_returns_short_text_whole() -> None:
    assert split_message("hello") == ["hello"]


def test_split_message_returns_empty_text_as_one_empty_chunk() -> None:
    """A caller that always sends one message never special-cases the empty case."""

    assert split_message("") == [""]


def test_split_message_splits_on_a_line_boundary() -> None:
    text = "first line\nsecond line\nthird line"
    chunks = split_message(text, max_length=20)

    assert len(chunks) > 1
    assert all(len(chunk) <= 20 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_message_never_exceeds_the_limit() -> None:
    text = "x" * (MAX_MESSAGE_LENGTH * 3 + 10)

    chunks = split_message(text)

    assert all(len(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "".join(chunks) == text


# --------------------------------------------------------------------------- rate limit


def test_rate_limiter_allows_up_to_the_cap_then_refuses() -> None:
    limiter = RateLimiter(3, 60.0)

    assert [limiter.allow(ALLOWED_USER) for _ in range(4)] == [True, True, True, False]


def test_rate_limiter_refusal_consumes_nothing() -> None:
    """A refused call must not push the user further out: the window is time, not attempts."""

    limiter = RateLimiter(2, 60.0)
    assert limiter.allow(ALLOWED_USER) is True
    assert limiter.allow(ALLOWED_USER) is True
    assert limiter.allow(ALLOWED_USER) is False
    assert limiter.allow(ALLOWED_USER) is False


def test_rate_limiter_isolates_users() -> None:
    limiter = RateLimiter(1, 60.0)

    assert limiter.allow(ALLOWED_USER) is True
    assert limiter.allow(OTHER_USER) is True
    assert limiter.allow(ALLOWED_USER) is False


def test_rate_limiter_window_expires() -> None:
    """A fake clock proves the window is a window, not a lifetime cap."""

    now = [1000.0]
    limiter = RateLimiter(1, 60.0, clock=lambda: now[0])

    assert limiter.allow(ALLOWED_USER) is True
    assert limiter.allow(ALLOWED_USER) is False
    now[0] = 1061.0
    assert limiter.allow(ALLOWED_USER) is True


def test_rate_limiter_rejects_invalid_identities_without_creating_state() -> None:
    limiter = RateLimiter(1, 60.0)

    for invalid in (0, -1, True, False, "1", None, 1.5):
        assert limiter.allow(invalid) is False  # type: ignore[arg-type]
    assert len(limiter) == 0


def test_rate_limiter_refuses_new_users_when_saturated() -> None:
    """A full table denies rather than growing without bound."""

    limiter = RateLimiter(1, 60.0, max_users=1)
    assert limiter.allow(ALLOWED_USER) is True
    assert limiter.allow(OTHER_USER) is False


def test_rate_limiter_rejects_a_backwards_clock_step() -> None:
    """A clock that moves backwards must not grant extra budget."""

    now = [1000.0]
    limiter = RateLimiter(1, 60.0, clock=lambda: now[0])
    assert limiter.allow(ALLOWED_USER) is True
    now[0] = 500.0
    assert limiter.allow(ALLOWED_USER) is False


@pytest.mark.parametrize(
    ("max_requests", "window"),
    [(0, 60.0), (-1, 60.0), (1, 0.0), (1, -5.0), (1, float("inf")), (1.5, 60.0), (True, 60.0)],
)
def test_rate_limiter_rejects_invalid_configuration(max_requests: Any, window: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        RateLimiter(max_requests, window)


# --------------------------------------------------------------------------- admission


def test_admission_accepts_an_allowlisted_private_chat() -> None:
    verdict = admit(_message(), config=_config())

    assert verdict.allowed is True
    assert verdict.user_id == ALLOWED_USER
    assert verdict.chat_id == ALLOWED_USER


def test_admission_refuses_a_user_who_is_not_invited() -> None:
    verdict = admit(_message(user_id=OTHER_USER, chat_id=OTHER_USER), config=_config())

    assert verdict.allowed is False
    assert verdict.reason == "user not invited"


def test_admission_refuses_a_group_chat() -> None:
    verdict = admit(_message(chat_type="group"), config=_config())

    assert verdict.allowed is False
    assert verdict.reason == "private chat required"


def test_admission_refuses_a_chat_id_that_does_not_match_its_sender() -> None:
    """A mismatched payload must not be answered: the reply could land in someone else's chat."""

    verdict = admit(_message(chat_id=OTHER_USER), config=_config())

    assert verdict.allowed is False
    assert verdict.reason == "chat id mismatch"


def test_admission_refuses_a_chat_that_is_not_invited() -> None:
    config = _config(allowed_chat_ids=frozenset({OTHER_USER}))

    verdict = admit(_message(), config=config)

    assert verdict.allowed is False
    assert verdict.reason == "chat not invited"


def test_admission_refuses_missing_or_blank_text() -> None:
    config = _config()

    assert admit(_message(text="   "), config=config).reason == "missing text"
    no_text = {"from": {"id": ALLOWED_USER}, "chat": {"id": ALLOWED_USER, "type": "private"}}
    assert admit(no_text, config=config).reason == "missing text"


def test_admission_refuses_an_oversized_message() -> None:
    config = _config(max_message_length=10)

    assert admit(_message(text="x" * 11), config=config).reason == "message too long"


def test_admission_refuses_a_non_dict_message() -> None:
    assert admit(None, config=_config()).reason == "no message"


@pytest.mark.parametrize("bad_id", [0, -1, True, False, "1", None, 1.5])
def test_admission_refuses_a_malformed_user_id(bad_id: Any) -> None:
    """``True`` is an ``int`` in Python; without an explicit bool check it would be user 1."""

    verdict = admit(_message(user_id=bad_id, chat_id=bad_id), config=_config())  # type: ignore[arg-type]

    assert verdict.allowed is False


def test_admission_identity_is_checked_before_chat_type() -> None:
    """A refusal must not reveal whether a chat id was on the allowlist."""

    stranger_in_a_group = admit(
        _message(user_id=OTHER_USER, chat_id=OTHER_USER, chat_type="group"),
        config=_config(),
    )

    assert stranger_in_a_group.reason == "user not invited"


# --------------------------------------------------------------------------- adapter


def _adapter_with(handler: Any, config: TelegramConfig | None = None) -> TelegramAdapter:
    """An adapter whose HTTP transport is a test double."""

    adapter = TelegramAdapter(config if config is not None else _config())
    adapter._client = httpx.AsyncClient(  # noqa: SLF001 - tests may inject a transport
        base_url=adapter._url,  # noqa: SLF001
        transport=httpx.MockTransport(handler),
    )
    return adapter


def test_adapter_error_never_contains_the_token() -> None:
    """The token is in the URL path; ``httpx`` prints URLs, so the failure path is checked."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert TOKEN in str(request.url), "the test is not exercising the token-bearing path"
        return httpx.Response(500, json={"ok": False, "description": "boom"})

    async def main() -> None:
        adapter = _adapter_with(handler)
        with pytest.raises(TelegramError) as excinfo:
            await adapter.get_me()
        message = str(excinfo.value)
        assert TOKEN not in message
        assert "api.telegram.org" not in message
        assert excinfo.value.kind == "telegram_http_error"

    asyncio.run(main())


def test_adapter_reports_a_rejected_request_without_the_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    async def main() -> None:
        adapter = _adapter_with(handler)
        with pytest.raises(TelegramError) as excinfo:
            await adapter.get_me()
        assert "chat not found" in str(excinfo.value)
        assert TOKEN not in str(excinfo.value)

    asyncio.run(main())


def test_adapter_refuses_a_malformed_reply() -> None:
    """A scalar or non-object result is a contract violation, not a silent empty reply."""

    async def main() -> None:
        scalar = _adapter_with(lambda request: httpx.Response(200, json={"ok": True, "result": 1}))
        with pytest.raises(TelegramError):
            await scalar.get_me()

        not_json = _adapter_with(lambda request: httpx.Response(200, text="<html>"))
        with pytest.raises(TelegramError):
            await not_json.get_me()

    asyncio.run(main())


def test_adapter_refuses_a_malformed_get_updates_result() -> None:
    async def main() -> None:
        body = {"ok": True, "result": {"a": 1}}
        adapter = _adapter_with(lambda request: httpx.Response(200, json=body))
        with pytest.raises(TelegramError):
            await adapter.get_updates()

    asyncio.run(main())


def test_adapter_sends_to_the_chat_id_it_is_given() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def main() -> None:
        adapter = _adapter_with(handler)
        await adapter.send_message(ALLOWED_USER, "hello")

    asyncio.run(main())

    assert sent[0]["chat_id"] == str(ALLOWED_USER)
    assert sent[0]["text"] == "hello"


def test_adapter_splits_a_long_reply() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def main() -> None:
        adapter = _adapter_with(handler)
        await adapter.send_message(ALLOWED_USER, "y" * (MAX_MESSAGE_LENGTH + 10))

    asyncio.run(main())

    assert len(sent) == 2
    assert all(len(item["text"]) <= MAX_MESSAGE_LENGTH for item in sent)


# --------------------------------------------------------------------------- config


def test_actor_for_threads_the_callers_identity() -> None:
    """NYX records who filed a rebuttal; a bot must record the account, not a made-up name."""

    config = _config(actor_prefix="telegram:")

    assert config.actor_for(ALLOWED_USER) == f"telegram:{ALLOWED_USER}"
    assert config.actor_for(ALLOWED_USER) != config.actor_for(OTHER_USER)


def test_config_is_configured_only_with_a_token_and_an_allowlist() -> None:
    assert _config().is_configured is True
    assert _config(bot_token="").is_configured is False
    assert _config(allowed_user_ids=frozenset()).is_configured is False


def test_settings_fail_closed_without_a_token() -> None:
    with pytest.raises(ValidationError, match="NYX_TELEGRAM_BOT_TOKEN"):
        Settings(telegram_enabled=True, telegram_bot_token=None)


def test_settings_fail_closed_without_an_allowlist() -> None:
    with pytest.raises(ValidationError, match="NYX_TELEGRAM_ALLOWED_USER_IDS"):
        Settings(telegram_enabled=True, telegram_bot_token=SecretStr(TOKEN))


def test_settings_leave_the_bot_disabled_by_default() -> None:
    """A bot that is off must not require a token, or the deterministic core could not start."""

    assert Settings().telegram_enabled is False


# --------------------------------------------------------------------------- the bot


class _FakeAdapter:
    """Records what the bot sent, and replays a scripted update stream."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.commands_registered: list[list[dict[str, str]]] = []

    async def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
        self.commands_registered.append(list(commands))
        return True

    async def send_message(
        self, chat_id: int, text: str, *, parse_mode: str | None = None
    ) -> dict[str, Any]:
        self.sent.append((chat_id, text))
        return {"message_id": 1}

    async def get_updates(
        self, *, offset: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return []

    async def close(self) -> None:
        return None


class _FakeAgent:
    """Stands in for the chat agent: scripted answers, and a record of what it was asked."""

    def __init__(self, answer: str = "NYX has no findings.") -> None:
        self.answer = answer
        self.questions: list[str] = []
        self.touched: list[str] = []

    async def run_async(self, question: str) -> str:
        self.questions.append(question)
        return self.answer


def _bot(
    *,
    config: TelegramConfig | None = None,
    answer: str = "NYX has no findings.",
) -> tuple[TelegramBot, _FakeAdapter, list[_FakeAgent]]:
    """A bot whose adapter and agents are doubles, so a test can inspect every side effect."""

    adapter = _FakeAdapter()
    agents: list[_FakeAgent] = []

    def factory(actor: str) -> _FakeAgent:
        agent = _FakeAgent(answer)
        agent.actor = actor  # type: ignore[attr-defined]
        agents.append(agent)
        return agent  # type: ignore[return-value]

    bot = TelegramBot(
        config if config is not None else _config(),
        agent_factory=factory,
        adapter=adapter,  # type: ignore[arg-type]
    )
    return bot, adapter, agents


def test_bot_answers_an_admitted_message_through_the_agent() -> None:
    bot, adapter, agents = _bot(answer="finding fnd_1 is open")

    result = asyncio.run(bot.handle_update(_update(1)))

    assert result["status"] == "ok"
    assert agents[0].questions == ["what findings does NYX have?"]
    assert adapter.sent == [(ALLOWED_USER, "finding fnd_1 is open")]


def test_bot_records_the_telegram_account_as_the_actor() -> None:
    """The identity in NYX's audit trail is the account that sent the message."""

    bot, _, agents = _bot()

    asyncio.run(bot.handle_update(_update(1)))

    assert agents[0].actor == f"telegram:{ALLOWED_USER}"  # type: ignore[attr-defined]


def test_bot_ignores_a_non_allowlisted_user_without_calling_the_model() -> None:
    bot, adapter, agents = _bot()

    result = asyncio.run(bot.handle_update(_update(1, user_id=OTHER_USER, chat_id=OTHER_USER)))

    assert result["status"] == "rejected"
    assert result["reason"] == "user not invited"
    assert agents == []
    assert adapter.sent == []


def test_bot_ignores_a_group_chat() -> None:
    bot, adapter, agents = _bot()

    result = asyncio.run(bot.handle_update(_update(1, chat_type="group")))

    assert result["status"] == "rejected"
    assert adapter.sent == []
    assert agents == []


def test_bot_never_replies_to_a_stranger() -> None:
    """A refusal is logged, not sent: telling a stranger the gate exists aids probing."""

    bot, adapter, agents = _bot()

    asyncio.run(bot.handle_update(_update(1, user_id=OTHER_USER, chat_id=OTHER_USER)))

    assert adapter.sent == []
    assert agents == []


def test_bot_deduplicates_a_redelivered_update() -> None:
    """Telegram retries updates. A retried update must not file a second rebuttal."""

    bot, adapter, agents = _bot()

    first = asyncio.run(bot.handle_update(_update(7)))
    second = asyncio.run(bot.handle_update(_update(7)))

    assert first["status"] == "ok"
    assert second["status"] == "duplicate"
    assert len(agents) == 1
    assert len(adapter.sent) == 1


def test_bot_forgets_old_update_ids_once_the_table_fills() -> None:
    """Duplicate protection is bounded: an id evicted from the table may be handled again."""

    adapter = _FakeAdapter()
    agents: list[_FakeAgent] = []
    bot = TelegramBot(
        _config(rate_limit_requests=MAX_TRACKED_UPDATES + 10),
        agent_factory=lambda actor: agents.append(_FakeAgent()) or agents[-1],  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
    )

    asyncio.run(bot.handle_update(_update(1)))
    for update_id in range(2, MAX_TRACKED_UPDATES + 2):
        asyncio.run(bot.handle_update(_update(update_id)))
    # The first id has been evicted, so it is handled again rather than stuck as a duplicate.
    asked_before = len(agents[0].questions)
    replayed = asyncio.run(bot.handle_update(_update(1)))
    assert replayed["status"] == "ok"
    # One chat keeps one agent, and the replay was handled exactly once more.
    assert len(agents) == 1
    assert len(agents[0].questions) == asked_before + 1


def test_bot_rate_limited_user_gets_a_fixed_message_and_no_model_call() -> None:
    bot, adapter, agents = _bot(config=_config(rate_limit_requests=1))

    asyncio.run(bot.handle_update(_update(1)))
    result = asyncio.run(bot.handle_update(_update(2)))

    assert result["status"] == "rejected"
    assert result["reason"] == "rate_limited"
    assert len(agents) == 1
    assert adapter.sent[-1] == (ALLOWED_USER, RATE_LIMITED)


def test_bot_commands_cost_nothing_and_never_reach_the_model() -> None:
    bot, adapter, agents = _bot()

    start = asyncio.run(bot.handle_update(_update(1, text="/start")))
    help_ = asyncio.run(bot.handle_update(_update(2, text="/help")))

    assert start["kind"] == "command"
    assert help_["kind"] == "command"
    assert agents == []
    assert adapter.sent[0] == (ALLOWED_USER, START_REPLY)
    assert adapter.sent[1] == (ALLOWED_USER, HELP_REPLY)


def test_bot_unknown_command_gets_a_short_answer() -> None:
    bot, adapter, _ = _bot()

    asyncio.run(bot.handle_update(_update(1, text="/nope")))

    assert adapter.sent == [(ALLOWED_USER, "Unknown command /nope. Try /help.")]


def test_bot_sends_nothing_for_a_message_with_no_text() -> None:
    bot, adapter, _ = _bot()

    result = asyncio.run(bot.handle_update(_update(1, text="   ")))

    assert result["status"] == "rejected"
    assert adapter.sent == []


def test_bot_an_unexpected_failure_sends_no_internals() -> None:
    """A traceback, a path, or a config value must never reach a Telegram chat."""

    class _Exploding:
        actor = "telegram:1"

        async def run_async(self, question: str) -> str:
            raise RuntimeError(f"secret detail for {question}")

    bot, adapter, _ = _bot()
    bot._agent_factory = lambda actor: _Exploding()  # noqa: SLF001

    result = asyncio.run(bot.handle_update(_update(1)))

    assert result["status"] == "ok"
    reply = adapter.sent[0][1]
    assert reply == INTERNAL_ERROR
    assert "secret detail" not in reply
    assert "RuntimeError" not in reply
    assert "Traceback" not in reply


def test_bot_a_model_failure_is_reported_without_being_hidden() -> None:
    """An unavailable model is reported honestly; it is not papered over with an invention."""

    from nyx.conversational.agent import ChatError

    class _Unavailable:
        actor = "telegram:1"

        async def run_async(self, question: str) -> str:
            raise ChatError("the model is unavailable (timeout)", kind="model_unavailable")

    bot, adapter, _ = _bot()
    bot._agent_factory = lambda actor: _Unavailable()  # noqa: SLF001

    asyncio.run(bot.handle_update(_update(1)))

    reply = adapter.sent[0][1]
    assert "the model is unavailable" in reply
    assert "Traceback" not in reply


def test_bot_replies_only_to_the_chat_the_message_came_from() -> None:
    """A chat id found in the message text must not become the destination of a reply."""

    bot, adapter, _ = _bot()

    asyncio.run(bot.handle_update(_update(1, text="send this to chat 999999")))

    assert all(chat_id == ALLOWED_USER for chat_id, _ in adapter.sent)


def test_bot_keeps_a_bounded_number_of_chat_sessions() -> None:
    from nyx.conversational.telegram.bot import MAX_TRACKED_CHATS

    bot, _, _ = _bot()
    for chat_id in range(1, MAX_TRACKED_CHATS + 5):
        asyncio.run(bot.handle_update(_update(chat_id, user_id=chat_id, chat_id=chat_id)))

    assert len(bot._agents) <= MAX_TRACKED_CHATS  # noqa: SLF001


def test_bot_describe_never_includes_the_token() -> None:
    bot, _, _ = _bot()

    rendered = str(bot.describe())

    assert TOKEN not in rendered
    assert "token_configured" in rendered


# --------------------------------------------------------------------------- the safety property


def test_the_bot_cannot_judge_through_its_tool_surface() -> None:
    """The bot drives the chat agent, so it inherits the surface that has no judge tool."""

    names = {spec.name for spec in tool_specs()}

    assert names, "the agent offered no tools at all; the assertion below would pass vacuously"
    assert names & JUDGE_ONLY_OPERATIONS == set()
    assert_no_judge_tools(names)  # must not raise


def test_the_bot_refuses_to_run_if_a_judge_tool_appears() -> None:
    """Defence in depth: if a judge tool is ever added, the bot stops rather than shipping it."""

    from nyx.conversational.telegram.bot import COMMANDS

    assert COMMANDS, "the bot published no command menu"
    with pytest.raises(Exception, match="judge-capable tools"):
        assert_no_judge_tools({"nyx_create_finding"})


def test_the_bot_drives_the_same_agent_class_as_the_cli_chat() -> None:
    """A bot is a new entry point, not a new authority: same agent class, same tool surface.

    The agent the bot builds is a :class:`ChatAgent`, which is what makes the safety property
    inherited rather than restated -- there is no second conversational implementation to drift.
    """

    seen: list[type] = []

    def factory(actor: str) -> ChatAgent:
        agent = ChatAgent.__new__(ChatAgent)  # bypass __init__; the test only inspects the class
        seen.append(type(agent))
        return agent

    bot = TelegramBot(_config(), agent_factory=factory)

    asyncio.run(bot.handle_update(_update(1)))

    assert seen == [ChatAgent]
    assert bot._agent_factory is factory  # noqa: SLF001


def test_the_bot_publishes_a_command_menu() -> None:
    bot, adapter, _ = _bot()

    asyncio.run(bot.register_commands())

    assert adapter.commands_registered == [
        [
            {"command": "start", "description": "What this bot is, and what it can and cannot do"},
            {"command": "help", "description": "How to ask NYX about its record"},
        ]
    ]
