# Live probes

These are **independent** end-to-end probes. They deliberately do not use the project's own test
helpers: they talk to a real uvicorn server over real HTTP, or spawn the real MCP server process
and speak JSON-RPC to it over stdio. A bug in a test harness cannot hide a bug in the service, and
a mock transport cannot hide a bug in the wire format.

| Probe | What it proves |
| --- | --- |
| `probe_live_api.py` | The HTTP API: health, system registration, secret redaction, idempotent replay, structured errors. |
| `probe_mcp_live.py` | The MCP server over a **real stdio session** against a live NYX: handshake, tool surface, the forbidden-tool safety property, refusals arriving as results. |
| `probe_mcp_roundtrip.py` | A real finding round-trip: seeds data, runs the critics, reads it all back through MCP, files a rebuttal, and asserts append-only history. |
| `probe_critic_pack.py` | The whole critic pack runs without a 500, including C-010 against NYX's own store. |
| `probe_telegram_live.py` | The Telegram bot's **poll loop over real HTTP**: admission, dedupe of a redelivered update, a reply to exactly one admitted user, and the token never appearing in a payload. |

## Running them

Each probe expects a live NYX on `127.0.0.1:8000` (override with `NYX_PORT` for the API probes).
Start one from the repository root:

```bash
uv run alembic upgrade head          # against the database in .env
uv run python -m uvicorn nyx.api.app:create_app --factory --port 8000
```

Then, in another shell:

```bash
uv run python scripts/probe_live_api.py
uv run python scripts/probe_mcp_live.py
uv run python scripts/probe_mcp_roundtrip.py
uv run python scripts/probe_critic_pack.py
uv run python scripts/probe_telegram_live.py
```

Each exits non-zero on the first failing check and prints a `PROBE PASSED` / `PROBE FAILED` line.

## Why these exist

`probe_mcp_live.py` was written to close the verification gap the handoff recorded: the MCP
server's unit tests use a mock transport, so the stdio transport against a live NYX had never been
driven. Driving it found four real defects that every unit test had passed over:

- `list_critics` read the wrong response key (`critics`, not `items`), so a healthy 200 was
  reported as `INVALID_OUTPUT` and the tool was unusable end-to-end.
- An empty result serialised to an **empty string**, which a model cannot distinguish from a
  failed call.
- A missing record serialised to `null` text rather than an explicit "absent" result.
- `get_history` did not normalise `NOT_FOUND` to the absence sentinel the other getters honour.

`probe_critic_pack.py` found a fifth, in the deterministic core rather than the conversational
layer: C-010's violation finding named no claim or event, so the composer rejected it, and the
rejection escaped the registry's per-critic guard — turning a routine whole-pack run into a 500.

`probe_telegram_live.py` stands up a small HTTP server that speaks just enough of the Bot API and
points a real bot at it over a real socket, so it exercises the URL shape, the update parsing, and
the poll loop that a fake adapter cannot reach. It needs no secret and spends no money: the token
is a dummy and the agent is a double.

Running it found one real defect: the poll loop had **no backoff**, so a persistent `getUpdates`
failure turned into a hot loop hammering the Bot API as fast as it could turn. A poller that cannot
reach Telegram should be patient, not busy — it now waits `POLL_RETRY_SECONDS` before retrying.

The lesson is the one the handoff records: **a module's tests were written by whoever wrote the
module.** Build the probe outside the project's helpers, and assert on the wire shape.
