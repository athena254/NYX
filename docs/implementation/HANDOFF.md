# Handoff — NYX, 20 October 2026

**For the next session.** Read this first, then `docs/implementation/roadmap.md` and
`docs/implementation/acceptance-tests.md`.

**Repository:** `D:\NYX`, branch `master`, remote `github.com/athena254/NYX` (private).
**Head:** `e59a8cb`. **Working tree:** clean. **Gates:** 291 tests pass, mypy strict clean
(95 files), ruff clean, `alembic upgrade head` clean.

---

## 1. Where the project stands

Phases 0–3 are **done**. Phase 7 (conversational interface) is **in progress**, two slices in.

| Phase | State | Acceptance scenarios |
| --- | --- | --- |
| 0 Repository discovery | done | — |
| 1 Evidence and event spine | done | 1, 2, 3, 14, 18 |
| 2 Deterministic critic engine | done | 4–9, 12 (24 partial) |
| 3 Disputes and verification | done | 10, 11, 22, 23 |
| 4 Security and operational hardening | not started | 13–20 |
| 5 Integrations | not started | — |
| 6 Optional model critics | not started | 19 |
| 7 Conversational interface | **in progress** | none numbered |

Phase 7 slices landed:

- **6.3 LLM provider adapter** (`73d81f1`) — one interface, two wire protocols.
- **6.2 NYX MCP server** (`9ad7635`) — 12 tools over a thin HTTP client.
- **Security fix** (`e59a8cb`) — credentials in `base_url` are now refused, not leaked.

## 2. The rule that governs all Phase 7 work

> **The language model is the interface, never the judge.**

NYX's value is that its verdicts are deterministic and evidence-linked. A model may query, explain,
challenge, and ask for verification. It may **never** create a finding, record a disposition, or
change a lifecycle state.

This is enforced by the tool surface, not by prompt wording: `src/nyx/conversational/mcp_server.py`
has a `FORBIDDEN_TOOLS` tuple and a parametrised test asserting each id is **absent**. Adding a
dangerous tool must fail the build.

**Do not weaken that test, and do not add a tool without asking whether a human could do it by hand.**

## 3. What is left in Phase 7

| Item | State | Notes |
| --- | --- | --- |
| 6.1 Harden the CLI for other systems | **not started** | Mostly true already: all 15 commands have `--json`, 21 exit-code paths, no prompts. Needs a contract test asserting it. |
| 6.4 Conversational front-end | **not started** | `nyx chat` first, then a Telegram bot. This is what makes it usable. |
| 6.6 Documentation | **not started** | `docs/conversational-interface.md`. |
| dmr-X gateway | **not running** | Blocks end-to-end proof of 6.3. See §5. |
| End-to-end MCP run | **not done** | Unit tests use a mock transport; the stdio transport against a live NYX has never been driven. |

## 4. Placement and independence (decided, do not re-litigate)

- The conversational layer lives **inside `D:\NYX`** as a separate independent unit:
  `src/nyx/conversational/`. It imports nothing from `nyx.critics`, `nyx.findings`,
  `nyx.lifecycle`, `nyx.policies`, or `nyx.storage`, and reaches core only through the HTTP client.
- Its dependencies are an **extra**: `pip install nyx[conversational]` pulls in `mcp`. The
  deterministic core does not require it.
- **Never vendor another project's code.** dmr-X, The-Agency, and Chimera are consumed over HTTP.
  Port the *pattern*, never copy source. Both directions hold.

## 5. dmr-X — the blocker, and its exact state

dmr-X is the **default inference backend**: it holds the API keys and exposes the endpoint, so NYX
needs no key of its own. Confirmed endpoints from its route registration (`apps/gateway/src/server.ts`
registers both under a `/v1` prefix):

- `POST /v1/chat/completions` — OpenAI-compatible
- `POST /v1/messages` — Anthropic-native
- `/v1/models`

**It is not running.** Verified 20 October 2026:

- `.env` sets `PORT=3000`; nothing listening there.
- `DMRX_MCP_PORT=47114`, `DMRX_MCP_TRANSPORT=stdio`; nothing on 47114.
- PM2 — its declared supervisor (`ecosystem.config.cjs`) — is **not installed**.
- No build output (`apps/gateway/dist` absent); source-only.
- Companion ports down: 3100 (MCP/A2A sidecar), 7860 (G0DM0D3), 8011 (needle-router), 47113.

To run it:

```bash
cd C:\Users\PC\dmr-X
bun install
bun run build
bun run start          # gateway on :3000
```

`bun` 1.4.3 is present. **Starting it is the user's call** — building a large TypeScript monorepo on
this machine (2 cores, chronically tight RAM) is a significant operation. Ask before kicking it off.

## 6. Environment facts that will save time

- **The CLI is not a remote interface.** Measured ~**2.4s per invocation** even bare (`.venv/Scripts/nyx.exe`),
  because every command imports FastAPI and SQLAlchemy. Fine for scripts, unusable for an agent loop.
  This is why MCP goes over HTTP. Do not "simplify" by shelling out to the CLI from the tool layer.
- **mcp 2.x renamed `FastMCP` → `MCPServer`** (`mcp.server.mcpserver`). Use `MCPServer` and
  `@server.tool(...)`; `mcp.server.fastmcp` no longer exists.
- **OpenCode** is at `/c/Users/PC/AppData/Roaming/npm/opencode` (v1.19.0). Free Zen models that
  **responded** when probed: `deepseek-v4.1-flash-free`, `longcat-2.5-preview-free`,
  `glm-5.3-flash-free`. `qwen3.8-flash` and `kimi-k3` errored. Probe before delegating — free tiers
  saturate and a dead model looks identical to a stall.
- **Never ask the user to paste an API key into chat.** Tell them to write it into `.env` themselves.
- **A delegated agent's report is a claim.** Verify with `git status`, its own gates run by you, and
  a probe built outside the project's test helpers. One delegated slice passed every test it wrote
  while leaking a credential in a request header its tests never inspected (see §7).

## 7. The most important lesson from this session

A subagent implemented the LLM adapter and its 22 tests all passed. An **independent probe** then
found that `httpx` turns `https://user:pass@host` into a Basic auth header on its own — so a password
in `base_url` went out on the wire, in a header no test looked at.

The agent's tests asserted that error *messages* omit credentials. True, and satisfied — while the
credential was leaving in a request header. **A module's tests were written by whoever wrote the
module; one bad shared assumption hides the same bug everywhere.**

Build the probe outside the project's helpers, put the secret only in the input, and assert it appears
in none of the outputs: request URL, request headers, error text, logs.

Also: if you embed the secret in the string you search for it in, you will report a leak you caused.
That happened here once, and the corrected probe is the one to trust.

## 8. Conventions that must not break

From `AGENTS.md` and the `nyx-development` skill — breaking any of these is a regression:

- **Append-only.** No update or delete path for findings, rebuttals, dispositions, verification jobs,
  or policy versions. A disposition supersedes by storing the previous id; the previous row is never
  rewritten.
- **All state changes go through `FindingLifecycle`.** No code assigns `finding.status` directly.
- **Transition rules take the session:** `Callable[[Session, Finding, TransitionRequest], None]`.
- **Timestamps on recorded events are required, not defaulted.**
- **Secrets from environment/`.env` only.**
- **Never hardcode an actor or principal** — thread the caller's identity through.

## 9. Gate set

Run all of them, in this order, and read the summary line. Do not run a subset because "only tests
changed". Piping a gate into a filter hides its exit code — read `${PIPESTATUS[0]}` or drop the pipe.

```bash
uv run pytest
uv run mypy
uv run ruff check .
uv run ruff format --check .
uv run alembic upgrade head
```

## 10. Suggested next move

1. **Drive the MCP server against a live NYX process** — closes the last verification gap in 6.2 and
   takes under an hour. Start NYX, run `python -m nyx.conversational.mcp_server`, and confirm a tool
   call round-trips.
2. **Then 6.4 — `nyx chat`.** This is what makes the project usable by a human, and it is the piece
   the user has asked for twice.
3. **dmr-X** when the user says go, to prove 6.3 against a real endpoint.

Phase 4 (security hardening: real authentication, rate limits, audit-chain verification) is the other
large block of remaining work, and it is what makes NYX safe to expose beyond localhost.

---

## Update — 20 October 2026 (later session)

Steps 1 and 2 above are **done**. Commit `521d9c7` (MCP verification + five defect fixes) and the
chat work that followed.

**The MCP gap is closed.** `scripts/probe_mcp_live.py` spawns the real server and speaks JSON-RPC to
it over stdio. Three more probes joined it (`scripts/README-live-probes.md`). All four are green.

**Five real defects, all found by probes the unit tests could not catch:**

| # | Defect | Layer |
| --- | --- | --- |
| 1 | `list_critics` read `items` instead of `critics` — healthy 200 reported as `INVALID_OUTPUT` | conversational |
| 2 | An empty result serialised to an **empty string** | conversational |
| 3 | A missing record serialised to `null` text, not an explicit absence | conversational |
| 4 | `get_history` did not normalise `NOT_FOUND` to the absence sentinel | conversational |
| 5 | **C-010's violation finding could never be recorded** — it named no claim/event, so the composer rejected it, and the rejection escaped the registry's per-critic guard. One inconsistent store turned a routine whole-pack run into a **500**. | **deterministic core** |

**#5 is the one that matters.** It is in `nyx.critics.c010_nyx_self_consistency`, not the shell:
the critic whose entire job is to report an inconsistent store could not report one.

**`nyx chat` is built and working** (`docs/conversational-interface.md`):

```bash
nyx chat ask "What findings does NYX have?" --json
nyx chat shell
```

`src/nyx/conversational/agent.py` holds the loop. The safety property is tested from three
directions — the offered tool specs, a guard that refuses to start if a judge tool appears, and the
refusal a model actually receives. Three more defects surfaced while writing those tests: a
malformed tool call raised `KeyError` instead of returning a result, and the assistant's answer was
never kept in the transcript so a follow-up turn had no memory of what was said.

**Honest limitations, recorded in the docs rather than hidden:**

- Local reasoning models spend their budget on a `reasoning` field the adapter ignores. A full turn
  with `qwen3.5:0.8b` takes **30–90 seconds**.
- **A model can still embellish.** Observed live: asked about a finding, it appended "recommended
  actions" NYX never returned. The ids it cited were real; the prose around them was invented. The
  prompt forbids this; nothing structural prevents it yet.

**Gates:** 323 tests pass, mypy strict clean (96 files), ruff clean, `alembic upgrade head` clean.

**dmr-X is still not running** — that is unchanged, and remains the user's call. The chat was
exercised against local Ollama over the same OpenAI-compatible wire.

---

## Update — 21 October 2026

Phase 7 slices **6.1 (CLI contract)** and **6.6 (docs)** are done. Head `8c0cf38` was the previous
session's chat commit; this session adds two commits on top of it.

### 6.1 — the CLI contract, and three defects it found

`tests/integration/test_cli_contract.py` (12 tests) asserts the machine interface: every command
takes `--json`, the flag is a boolean switch, `--json` stdout is exactly one JSON document, failures
exit non-zero with a stable code and no traceback, and no command prompts. `docs/CLI-CONTRACT.md`
documents the exit codes and the per-item error shapes for callers.

Writing that test found **three defects, two of them contract violations**:

| # | Defect | Layer |
| --- | --- | --- |
| 1 | **A missing input file printed a traceback.** `nyx ingest event missing.json` raised `FileNotFoundError` — a raw Python exception, no error code, no exit code a caller could branch on. Now `INVALID_REQUEST` naming the path. | CLI |
| 2 | **`nyx chat` printed a pydantic traceback instead of refusing.** `NYX_LLM_ENABLED=true` with no model made `Settings()` raise `ValidationError`, which escaped `_require_llm()` as a traceback. The shell's own friendly refusal never ran. Now the named-variable message, as documented. | CLI |
| 3 | **A finding whose scope narrows by time could not be composed at all.** `dedupe_key` passed raw `datetime` objects into `canonical_json`, which had no default serializer, so `Scope.period` raised `TypeError` *after* the critic had already run. The finding was lost — not mislabelled. Any claim with a `period` hit this. | **deterministic core** |

**#3 is the important one, and it is the same shape as #5 above.** `canonical_json` is documented as
accepting "Pydantic models ... Datetimes use the domain's UTC `Z` representation", and it did — for a
model at the *top level*. A model nested inside a plain dict was never reached by the top-level dump,
so its datetimes arrived as `datetime` objects. A hash that is supposed to be a pure function of its
input failed on ordinary data. The fix is a `default=` handler on the encoder
(`src/nyx/domain/canonical.py`) plus `tests/unit/test_dedupe_key.py` (5 tests) asserting the key is
stable, differs when the period differs, differs when the critic differs, and that an unencodable
value still fails.

Reproduced with an independent probe before fixing (`tmp/probes/probe_period.py`), outside the
project's test helpers — the same discipline as the five defects above.

### 6.6 — documentation

`docs/CLI-CONTRACT.md` (new) documents the CLI's machine interface for callers. `docs/conversational-interface.md`
already covered the chat; `docs/implementation/HANDOFF.md` and `roadmap.md` are updated here.

### What is left in Phase 7

| Item | State |
| --- | --- |
| 6.1 Harden the CLI for other systems | **done** — contract-tested, documented |
| 6.2 NYX MCP server | **done** — live-verified |
| 6.3 LLM provider adapter | **done** — unit-tested, two protocols |
| 6.4 Conversational front-end | **CLI chat done**; the Telegram bot is the remaining half |
| 6.6 Documentation | **done** |
| dmr-X gateway | not running; blocks live proof of 6.3 against the real backend |
| End-to-end MCP run | **done** — `scripts/probe_mcp_live.py` |

### Gates (21 October 2026)

`uv run pytest` **340 passed** · `uv run mypy` clean (96 files) · `uv run ruff check .` clean ·
`uv run ruff format --check .` 153 files formatted · `uv run alembic upgrade head` clean.

The four live probes (`probe_live_api`, `probe_mcp_live`, `probe_mcp_roundtrip`, `probe_critic_pack`)
all pass against the migrated dev database.

### Next move

The Telegram bot — the last half of 6.4, and the piece that makes NYX usable from a phone. It ports
The-Agency's proven stack (handler, rate limiting, budget, error privacy) by pattern, never by
vendoring its source.



---

## Update — 21 October 2026 (third session)

Phase 7 slice **6.4 is complete**: the Telegram bot is built, tested, and proven live. The chat
timeout default is now **300s**. Head `d0ce133` was the previous session's tip.

### 6.4 — the Telegram bot

`src/nyx/conversational/telegram/` — five small modules, each with one job:

| Module | Job |
| --- | --- |
| `config.py` | The bot's own config (plain values, no `Settings`), and the actor identity derivation. |
| `admission.py` | Who may talk to the bot. Private chats only, an explicit allowlist, chat id must match sender. |
| `rate_limit.py` | A bounded per-user sliding-window limiter (pattern ported from The-Agency's beta guard). |
| `adapter.py` | The Bot API over `httpx`, built so the token can never reach an error message. |
| `bot.py` | The poll loop: admit, answer, reply, and never leak. |

The bot drives the **same `ChatAgent`** as `nyx chat`. That is the whole design: a bot is a second
entry point onto one tool surface, not a second implementation of it, so the safety property is
inherited rather than restated. `nyx telegram serve` and `nyx telegram check` are the commands.

Three properties worth recording, because they are where a bot usually goes wrong:

1. **A redelivered update is not replayed.** Telegram retries updates, and a retried update that
   filed a rebuttal would file it twice. Update ids are remembered *before* anything happens.
2. **A reply goes only to the chat the message came from.** A chat id found in the message text
   is never used as a destination.
3. **Nothing internal is ever sent.** A failure produces a fixed sentence; the traceback goes to
   the server log. The token lives in the Bot API URL path, so every failure path raises without
   it — asserted directly by test, not by inspection.

**Fail closed, and for a stronger reason than the chat shell.** `NYX_TELEGRAM_ENABLED=true` with no
token, or no `NYX_TELEGRAM_ALLOWED_USER_IDS`, is a `ValidationError`. A bot without an allowlist is
a bot that answers anyone who finds it.

### Defects found

| # | Defect | Layer |
| --- | --- | --- |
| 1 | **The poll loop had no backoff.** A persistent `getUpdates` failure (a revoked token, a network partition) became a hot loop hammering the Bot API as fast as it could turn. Now waits `POLL_RETRY_SECONDS` (5s). | conversational |
| 2 | The probe's own fake server matched `getUpdates` on the raw path, so the query string made it miss and every poll 404'd. **A probe bug, not a product bug** — recorded because it is the same shape as the handoff's lesson. | probe |

**#1 was found by the live probe, not by the 65 unit tests.** That is the discipline the handoff
asks for: `scripts/probe_telegram_live.py` stands up a real HTTP server speaking just enough of
the Bot API and drives a real bot against it over a real socket.

### The timeout default is now 300s

`NYX_LLM_TIMEOUT_SECONDS` default raised `120` → **`300`** in `src/nyx/config.py`, `.env.example`,
and `docs/conversational-interface.md`. Measured against `qwen3.5:0.8b`: one turn took **2m07s**, so
120s is *just* too short and the turn failed as a timeout rather than returning the answer it had
already computed. 300s is one call with headroom, not a guess at how long a model "should" take.

### Gates (21 October 2026)

`uv run pytest` **405 passed** · `uv run mypy` clean (102 files) · `uv run ruff check .` clean ·
`uv run ruff format --check .` 161 files formatted · `uv run alembic upgrade head` clean.

`scripts/probe_telegram_live.py` passes: one admitted user answered once, a stranger and a group
chat refused, a redelivered update not replayed, the token absent from every outbound payload.

### What is left in Phase 7

| Item | State |
| --- | --- |
| 6.1 Harden the CLI for other systems | **done** |
| 6.2 NYX MCP server | **done** — live-verified |
| 6.3 LLM provider adapter | **done** — unit-tested, two protocols |
| 6.4 Conversational front-end | **done** — CLI chat + Telegram bot |
| 6.6 Documentation | **done** |
| dmr-X gateway | not running; blocks live proof of 6.3 against the real backend |

**dmr-X is the only remaining verification gap in Phase 7.** Everything else is built and proven.
Until the gateway runs, 6.3 has been exercised against Ollama over the same OpenAI-compatible wire,
not against dmr-X itself.

### Next move

1. **dmr-X** — start it, then point `NYX_LLM_BASE_URL` at its gateway and run `nyx chat` and
   `nyx telegram check` against the real backend. That closes 6.3.
2. **Phase 4** — security hardening: real authentication, rate limits on the API, audit-chain
   verification. This is the other large block of remaining work, and what makes NYX safe to expose
   beyond localhost.


---

## Update — 21 October 2026 (fourth session): dmr-X is running, and 6.3 is proven

**dmr-X is up on `127.0.0.1:3000`, and `nyx chat` answered a real question against it with no API
key at all.** Phase 7 is now complete.

### Getting dmr-X to run

Two blockers, both in the dmr-X repo rather than in NYX:

1. **`bun install` had not linked the workspace packages.** `node_modules/@dmr-x/` was empty and
   no package had a `dist/`, so `bun start` failed with `Cannot find module '@dmr-x/db'`.
2. **turbo was unusable.** `node_modules/.bin/turbo.exe` was an 8 KB shim, and the real
   `node_modules/@turbo/windows-64/bin/turbo.exe` failed with
   `error while loading shared libraries: ghostty-vt.dll`. That DLL is not on npm (404) and was
   never fetched, because bun does not run the platform package's install hook the way npm does.

**The fix was to bypass turbo, not repair it.** `tsconfig.json` files carry no `references`, so
`tsc -b` cannot orchestrate; instead the workspace was topologically sorted on its own
`@dmr-x/*` dependencies and each package compiled with `tsc -p` in order. **35 modules compiled.**
The script is not checked in — it was a one-off, and turbo remains the project's real build.

Two things worth knowing before starting dmr-X again:

- **Run it from the repo root**: `bun apps/gateway/src/main.ts`. `main.ts` parses `.env` from
  `process.cwd()`, and `bun run start` sets cwd to `apps/gateway`, where there is no `.env` — so it
  came up with `NODE_ENV` unset, concluded it was in production, and refused with
  `DMRX_CORS_ORIGIN must be set to explicit origins in production`.
- The missing `@turbo/windows-64` was installed with `bun add --no-save`, so dmr-X's
  `package.json` is untouched. `bun.lock` did change by one line (gateway `0.5.20` → `0.5.21`,
  matching the already-committed `package.json`).

### 6.3 — proven against the real backend

`nyx chat ask "What findings does NYX have? Cite their ids." --json` returned a real answer citing
real finding ids from NYX's own store, over the OpenAI-compatible wire, with **no `Authorization`
header**. The adapter's two protocols are both exercised against a live endpoint now.

### Keyless providers — researched and verified

| Backend | Base URL | Model | Key | Verified |
| --- | --- | --- | --- | --- |
| dmr-X gateway | `http://127.0.0.1:3000` | `openai-fast` (→ `gpt-oss-20b`), `gpt-oss-120b` | none | HTTP 200 |
| Pollinations Classic | `https://text.pollinations.ai/openai` | `openai-fast` (only) | none | HTTP 200, `user_tier: anonymous` |

Measured caveats, all of them live rather than documented:

- **dmr-X's free upstreams rate-limit hard.** `429` and `502 all_providers_failed` were both
  observed. The adapter reports `the model is unavailable (unavailable)` and the chat refuses to
  answer — correct behaviour, and the reason a turn can fail intermittently.
- **Only 2 of the 58 advertised dmr-X model ids are reliably keyless.** The rest failed with
  `all_providers_failed`.
- **Pollinations' new `gen.pollinations.ai/v1` requires a key.** Only the legacy
  `text.pollinations.ai/openai` lane is anonymous, and it serves exactly one model.
- Everything else (Groq, OpenRouter `:free`, Google AI Studio, Mistral, Cohere, Cloudflare, Hugging
  Face, SiliconFlow, Fireworks, Z.AI, SambaNova, Cerebras, NVIDIA NIM) needs a **free key** — still
  no card, and still a one-line config change.

`.env` now points at the gateway: `NYX_LLM_PROVIDER=dmr-x`, `NYX_LLM_BASE_URL=http://127.0.0.1:3000`,
`NYX_LLM_MODEL=openai-fast`. That is the documented design default; it was previously pointed at
Ollama only because dmr-X was not running.

### What is left in Phase 7

| Item | State |
| --- | --- |
| 6.1 Harden the CLI for other systems | **done** |
| 6.2 NYX MCP server | **done** — live-verified |
| 6.3 LLM provider adapter | **done** — **proven against the real dmr-X backend** |
| 6.4 Conversational front-end | **done** — CLI chat + Telegram bot |
| 6.6 Documentation | **done** |
| dmr-X gateway | **running** |

**Phase 7 is complete.** Nothing in it is left.

### Next move

**Phase 4 — security and operational hardening.** Real authentication (the capability checker is
still `DenyAllCapabilityChecker`), rate limits on the API, audit-chain verification
(`nyx audit verify`), retention and redaction jobs, and the prompt-injection/SSRF/path-traversal
tests. This is what makes NYX safe to expose beyond localhost, and it is the largest remaining
block of work. Acceptance scenarios 13–20 gate it.
