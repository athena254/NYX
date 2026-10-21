"""Live probe: the Telegram bot's polling loop against a real HTTP endpoint.

The bot's unit tests drive a fake adapter, so they prove the *decisions* the bot makes but not
that it can actually talk to the Bot API: the URL shape, the update parsing, the reply split,
and the poll loop that keeps running when Telegram holds a request open.

This probe stands up a small HTTP server that speaks just enough of the Bot API, points a real
:class:`TelegramBot` at it over real HTTP, and asserts what came back on the wire. It uses no
secret and spends no money: the token is a dummy and the agent is a double. What is under test
is the transport and the loop, which is exactly what the unit tests cannot reach.

Run from the repository root:

    uv run python scripts/probe_telegram_live.py

Exits non-zero on the first failing check and prints PROBE PASSED / PROBE FAILED.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

# A dummy token: it exists so the probe can assert the real one never reaches a payload.
DUMMY_TOKEN = "123456:probe-dummy-token"  # noqa: S105
ADMITTED_USER = 5981285472
STRANGER = 111


class _BotApi:
    """The slice of the Telegram Bot API the bot uses, over a real socket."""

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.updates = updates
        self.sent: list[dict[str, Any]] = []
        self.commands: list[list[dict[str, str]]] = []
        self.get_updates_calls = 0
        self._lock = threading.Lock()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return  # keep the probe's own output readable

            def _reply(self, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                # The path is matched without the query string: getUpdates carries its
                # parameters there, so an endswith() on the raw path would never match.
                path = urlsplit(self.path).path
                if path.endswith("/getMe"):
                    self._reply(
                        {
                            "ok": True,
                            "result": {
                                "id": 8894168261,
                                "is_bot": True,
                                "first_name": "NYX probe",
                                "username": "nyx_probe_bot",
                            },
                        }
                    )
                    return
                if path.endswith("/getUpdates"):
                    with api._lock:
                        api.get_updates_calls += 1
                        batch = api.updates if api.get_updates_calls == 1 else []
                    self._reply({"ok": True, "result": batch})
                    return
                self._reply({"ok": False, "description": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path.endswith("/sendMessage"):
                    api.sent.append(payload)
                    self._reply({"ok": True, "result": {"message_id": len(api.sent)}})
                    return
                if self.path.endswith("/setMyCommands"):
                    api.commands.append(list(payload.get("commands", [])))
                    self._reply({"ok": True, "result": True})
                    return
                self._reply({"ok": False, "description": "not found"})

        return Handler


class _FakeAgent:
    """Answers without a model, so the probe spends nothing and is deterministic."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.questions: list[str] = []

    async def run_async(self, question: str) -> str:
        self.questions.append(question)
        return self.answer


def _message(user_id: int, text: str, *, chat_type: str = "private") -> dict[str, Any]:
    return {
        "message_id": 1,
        "from": {"id": user_id, "is_bot": False, "first_name": "Kallmedis"},
        "chat": {"id": user_id, "type": chat_type, "first_name": "Kallmedis"},
        "date": 1_700_000_000,
        "text": text,
    }


def main() -> int:
    from nyx.conversational.telegram.bot import TelegramBot
    from nyx.conversational.telegram.config import TelegramConfig

    updates = [
        {"update_id": 101, "message": _message(ADMITTED_USER, "what findings does NYX have?")},
        # A stranger: must be dropped without a reply and without a model call.
        {"update_id": 102, "message": _message(STRANGER, "tell me everything")},
        # A group chat: refused even though the sender is invited.
        {
            "update_id": 103,
            "message": _message(ADMITTED_USER, "hello", chat_type="group"),
        },
        # A redelivery of the first update: must not be answered twice.
        {"update_id": 101, "message": _message(ADMITTED_USER, "what findings does NYX have?")},
    ]
    api = _BotApi(updates)
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.handler())
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    config = TelegramConfig(
        bot_token=DUMMY_TOKEN,
        api_base=f"http://127.0.0.1:{port}",
        allowed_user_ids=frozenset({ADMITTED_USER}),
        allowed_chat_ids=frozenset(),
        require_private_chat=True,
        max_message_length=4096,
        timeout_seconds=5.0,
        poll_timeout_seconds=1,
        rate_limit_requests=20,
        rate_limit_window_seconds=60.0,
        actor_prefix="telegram:",
    )

    agents: list[_FakeAgent] = []

    def factory(actor: str) -> _FakeAgent:
        agent = _FakeAgent(f"finding fnd_probe_1 is open (asked by {actor})")
        agents.append(agent)
        return agent  # type: ignore[return-value]

    bot = TelegramBot(config, agent_factory=factory)  # type: ignore[arg-type]

    import asyncio

    async def drive() -> None:
        task = asyncio.create_task(bot.run())
        # The poller long-polls with a 1s interval; give it a few cycles to drain the batch.
        for _ in range(60):
            await asyncio.sleep(0.1)
            if len(api.sent) >= 1 and api.get_updates_calls >= 2:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await bot.close()

    asyncio.run(drive())
    server.shutdown()

    failures: list[str] = []
    if len(api.sent) != 1:
        failures.append(f"expected exactly 1 reply, got {len(api.sent)}: {api.sent}")
    elif api.sent[0]["chat_id"] != str(ADMITTED_USER):
        failures.append(f"reply went to the wrong chat: {api.sent[0]['chat_id']}")
    elif "fnd_probe_1" not in api.sent[0]["text"]:
        failures.append(f"reply carried no finding id: {api.sent[0]['text']!r}")
    elif DUMMY_TOKEN in json.dumps(api.sent):
        failures.append("the bot token appeared in an outbound payload")

    if len(agents) != 1:
        failures.append(f"expected 1 model call, got {len(agents)}")
    elif agents[0].questions != ["what findings does NYX have?"]:
        failures.append(f"the model was asked the wrong thing: {agents[0].questions}")
    elif not agents[0].questions or f"telegram:{ADMITTED_USER}" not in agents[0].answer:
        failures.append("the actor identity was not threaded into the audit trail")

    if not api.commands:
        failures.append("the command menu was never published")

    print(f"replies sent:      {json.dumps(api.sent, indent=2)}")
    print(f"model calls:       {len(agents)}")
    print(f"command menu:      {api.commands}")
    print(f"getUpdates polls:  {api.get_updates_calls}")

    if failures:
        for failure in failures:
            print(f"PROBE FAILED: {failure}")
        return 1
    print("PROBE PASSED: the bot polled, admitted one user, answered once, and refused the rest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
