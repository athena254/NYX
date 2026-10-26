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
still DenyAllCapabilityChecker), rate limits on the API, audit-chain verification
(`nyx audit verify`), retention and redaction jobs, and the prompt-injection/SSRF/path-traversal
tests. This is what makes NYX safe to expose beyond localhost, and it is the largest remaining
block of work. Acceptance scenarios 13–20 gate it.

---

## Update — 21 October 2026 (fifth session): Phase 4 is complete

Phase 4 is **done**. Head `8c70cf6` was the previous session's tip; this session adds twelve commits.

### Where the project stands

| Phase | State | Acceptance scenarios |
| --- | --- | --- |
| 0 Repository discovery | done | — |
| 1 Evidence and event spine | done | 1, 2, 3, 14, 18 |
| 2 Deterministic critic engine | done | 4–9, 12 (24 partial) |
| 3 Disputes and verification | done | 10, 11, 22, 23 |
| 4 Security and operational hardening | **done** | 13–21 |
| 5 Integrations | not started | — |
| 6 Optional model critics | not started | 19 (live), 15 (model path) |
| 7 Conversational interface | done | none numbered |

### What was built

Eight slices: shared configuration; audit-chain verification; authentication and capability
enforcement; the injection/SSRF/path-traversal guards; rate limiting; retention and redaction; the
health/doctor and API/CLI consistency tests; the failure-safe LLM critic; CI and a container image.

### The discipline that mattered

Every slice was gated by running the project's own commands, and then by a **probe written outside
the project's test helpers** and driven against a real migrated database. That found nine defects
that the agents' own tests did not — including one where the plan itself was wrong (it claimed
existing tests guarded the audit hash chain; there were none) and one where a slice's own tests
passed while the product was broken (the capability checker was wired while the actor was still
self-asserted, so anyone could claim the `operator` identity).

Full list in `docs/implementation/roadmap.md` §Phase 4.

### Gates (21 October 2026)

`uv run pytest` **545 passed, 1 skipped** · `uv run mypy` clean (120 files) · `uv run ruff check .`
clean · `uv run ruff format --check .` 194 files formatted · `uv run alembic upgrade head` clean.

The one skip is a Windows platform skip in `tests/security/test_ssrf_and_paths.py` (symlink creation
is not permitted without privilege), not a stale SKIP.

### Honest limitations, recorded rather than hidden

- **The container image has never been built.** Docker is not installed on this machine. The
  `Dockerfile` is verified by inspection and because every command in it is one CI runs; the
  workflow builds and scans it.
- **Rate limiting is per-process.** A multi-process deployment needs a shared store.
- **Scenario 19 is checked for the stub path only.** The live provider adapter is Phase 6.
- **Scenario 24 is now checked.** C-010 is enforced as a gate on `confirmed` and
  `partially_confirmed`, so the last unchecked box is closed.
- **`nyx doctor` does not probe an LLM endpoint's reachability.** It reports what is configured and
  no longer claims an availability it never verified.

### Next move

1. **Phase 5 — integrations.** The Git/CI adapter first. Each adapter needs a contract test and a
   documented disablement path, and no sibling project's code is vendored — they are consumed over
   HTTP.
2. **Scenario 24**, if it is wanted: enforce C-010 as a gate on `confirmed`. It is the last
   unchecked box and it is a Phase 2 obligation.
3. **Build the image somewhere with Docker**, to close the one unverified step in CI.

---

## Update — 21 October 2026 (sixth session): scenario 24 is closed

The last unchecked acceptance box is closed, and four stale status entries were corrected.

### Scenario 24 — C-010 is now a gate, not only a critic

`nyx.findings.lifecycle` gained `_SELF_CONSISTENCY_GATED` (`confirmed`, `partially_confirmed`) and a
`self_consistency_gate` seam. The default gate runs C-010's checks for the finding on the caller's
own transaction, so the invariant is enforced with **no wiring at all** — there is no off switch.

Two design decisions worth recording:

- **The gate is narrower than the critic.** C-010's standalone run is store-global by design: a
  violation *anywhere* undermines trust in every confirmed finding. A **gate** has to be answerable
  to the caller, so it runs the same three check helpers against the one finding being confirmed,
  plus the audit-chain check, which is inherently store-global. The scope difference is documented
  in both modules rather than left to be discovered.
- **The import is lazy.** `nyx.critics.c010_nyx_self_consistency` imports `allowed_transitions` from
  `nyx.findings.lifecycle`, so a module-level import back would be circular. The default gate imports
  it inside the function.

### Two fixture defects the gate found

The gate refused findings the existing tests had always confirmed. Both were real inconsistencies in
the fixtures, not faults in the gate:

| # | Defect | Layer |
| --- | --- | --- |
| 1 | `tests/unit/test_finding_lifecycle.py` and `tests/integration/test_finding_lifecycle.py` cited evidence ids (`ev_test`, `ev_scenario_23`) that **no evidence row backed**. C-010 resolves a finding's evidence ids against the store, so every one of those findings was a self-consistency violation that nothing had ever detected. | tests |
| 2 | The lifecycle tests built `FindingLifecycle` directly and so never exercised the gate. They do now, precisely because the gate has no off switch. | tests |

### Status documents corrected

| Document | Was | Now |
| --- | --- | --- |
| `roadmap.md` summary | Phase 7 "in progress" | done |
| `task-checklist.md` step 12 | `[~]` — "chain verification is still Phase 4" | `[x]` |
| `task-checklist.md` step 13 | `[~]` | `[x]` |
| `task-checklist.md` step 14 | `[ ]` | `[x]` |
| `task-checklist.md` step 16 | `[ ]` | `[~]` (Phase 4 stub only) |
| `roadmap.md` Phase 2 | "capability seam is still DenyAll…", "step 12 remains open" | corrected |
| `acceptance-tests.md` | scenario 24 `[ ]` | `[x]` |

### Gates (21 October 2026)

`uv run pytest` **552 passed, 1 skipped** · `uv run mypy` clean (120 files) · `uv run ruff check .`
clean · `uv run ruff format --check .` 195 files formatted · `uv run alembic upgrade head` clean.

`tmp/probes/probe_scenario_24_gate.py` — written outside the project's test helpers, against a real
migrated database: **6 checks, all green**.

### Next move

**Phase 5 — integrations.** `docs/implementation/phase-5-plan.md` is being written; the Git/CI
adapter is the first slice.

---

## Update — 21 October 2026 (seventh session): the Phase 5 plan is accepted

`docs/implementation/phase-5-plan.md` is written, counterchecked against the code, and accepted. It
closes the Phase 0 open box (SPEC §12 dependency review) and defines six slices for the Git/CI
adapter.

### The countercheck found one false claim and two stale ones

The plan is a claim set, so it was checked against the code before being accepted rather than trusted:

| # | Plan claim | Verdict |
| --- | --- | --- |
| D4 | "`src/nyx/evidence/` does not exist" | **False.** `ls -la src/nyx/evidence/` shows an **empty directory**. It is untracked (git does not track empty directories), which is why `git log --all` was empty — but the directory is there. Corrected. |
| D6/D7 | the scenario-24 change is uncommitted | **Stale.** Written while that change was in flight; it is now committed as `b6b2858`. Resolved. |
| §2.2/§2.3 | code comments labelling `base.py` as slice 5.1, `config.py` as 5.2, `normalize.py` as 5.3, `adapter.py` as 5.4 | **Off by one** against §3's own slice table. Corrected. |

The rest of the plan's countercheck findings were **confirmed**: `task-checklist.md` step 15 names
`docs/adr/0007-git-ci-adapter.md` but ADR 0007 is `0007-auth-modes.md` (next free number is 0008);
step 6 names `src/nyx/evidence/*` which has no modules; `certifi 2026.10.19` is installed and
`uv audit` reports no known vulnerabilities in 70 packages; there is **no file adapter** in the code;
`httpx` is called only from `conversational/`.

### Decisions taken (the plan's open questions, now closed)

- **No new third-party dependency for Phase 5.** `httpx` (already installed, BSD-3-Clause) plus the
  standard library. `PyGithub` (LGPL-3.0, actively seeking maintainers), `pygit2` (GPL-2.0, one
  maintainer), `dulwich`, `GitPython`, and `requests` are recorded with their rejection reasons.
- **OpenTelemetry is out of Phase 5.** It is a separate adapter with its own dependency review.
- **The local-git provider uses no `subprocess`.** It reads `.git/HEAD`, `.git/refs/**`,
  `.git/packed-refs`, and `.git/logs/HEAD` with `pathlib` and the standard library, so gate G1 stays
  intact and scenario 16's hole stays closed by construction. **Documented limitation:** it reads refs
  and the reflog, not git objects, so it cannot read commit contents or diffs.
- **`adapter_enabled` is a deployment filter; the registry is the code inventory.** Keeping them apart
  is what lets `nyx doctor` say "registered but disabled" without repeating the Phase 4 defect of
  reporting configuration as availability.

### Status documents corrected

`task-checklist.md` step 15 (ADR number), step 6 (evidence paths), and the Phase 0 open box in
`roadmap.md` (the SPEC §12 review, now closed by the plan's §1).

### Next move

**Slice 5.0** — shared adapter infrastructure, orchestrator-owned: `src/nyx/adapters/{__init__,base}.py`,
the `Settings` fields, `.env.example`, the two new `ErrorCode` members, ADR 0008, and the boundary
grep test (G7). It must land **before** any delegated slice starts, or a child can create a file the
orchestrator then cannot write.

---

## Update — 22 October 2026 (eighth session): slices 5.0, 5.1, 5.2 landed

**Answer to "is slice 5.0 the last piece?" — no, it is the first.** Phase 5 has six slices
(5.0–5.5); Phase 6 (model critics and calibration) follows. What is left after this session:
5.3 adapter assembly + live contract test, 5.4 local-git provider, 5.5 fixture provider +
the disablement test (the Phase 5 exit criterion), then all of Phase 6.

### Landed

| Slice | Commit | What |
| --- | --- | --- |
| 5.0 | `c0bc402` | `Adapter` protocol, registry, health, config fields, ADR 0008, boundary grep (G7) |
| 5.1 + 5.2 | `f652fcb` | `GitHubCiClient`, `GitCiConfig`, `normalize.py` — built in parallel, disjoint files |

### The probe found a defect 28 unit tests could not

`tmp/probes/probe_git_ci_adapter.py` stands up a real HTTP server speaking enough of the GitHub
API, drives the real client over a real socket, and pushes the normalized output through the real
`IngestionService` against a real migrated database. **28 checks, all green.**

It failed first on a real defect: **the adapter's evidence could not be stored at all.**
`EvidenceItem` requires `relevance_to_claim` or `relevance_to_finding`, and an observing adapter
produces evidence *before* any claim references the run. Slice 5.2's 20 tests passed because they
asserted that `normalize()` returns valid Pydantic input models and never pushed the result through
the ingestion path.

### One decision the owner may want to reverse

Emitting a claim from the same CI run was considered and rejected — it would make C-001 assess a
claim against evidence from its own source, which SPEC §8 warns is not independent corroboration.

**Resolution (SPEC §5.4 deviation, recorded in ADR 0008):** `EvidenceItem` now accepts a third
state — neither relevance target, but a `relevance_note` explaining why. The original refusal is
intact for a producer that simply omits the field; unlinked evidence must *say it is unlinked*.
No column changed, no schema changed. `tests/unit/test_evidence_relevance.py` pins both directions.

**If the owner would rather keep SPEC §5.4 literally**, the alternatives are: have the monitored
system file the claim first and the adapter link to it (needs a claim-lookup the adapter cannot
do — it holds no session), or accept that the adapter emits events only and no evidence (then
C-001 has nothing to compare). Say which and it is a small change.

### Also corrected in the plan

- `EventInput` has **no `scope` field**, so §2.4's "set the head SHA on the event's `Scope`" was
  not implementable. The event carries it in `payload["commit"]`; the evidence on `Scope.commit`.
- The event and the evidence need **distinct** `producer_event_id`s, or the evidence's ingestion
  is a spurious `DUPLICATE_EVENT`.

### Gates (22 October 2026)

`uv run pytest` **603 passed, 1 skipped** · `uv run mypy` clean (126 files) · `uv run ruff check .`
clean · `uv run ruff format --check .` 205 files · `uv run alembic upgrade head` clean ·
`docs/openapi.json` unchanged.

### Next move

**Wave 2, in parallel:** slice 5.3 (`git_ci/adapter.py` + `tests/contract/test_git_ci_adapter.py`)
and slice 5.4 (`git_ci/local_git.py` + `tests/unit/test_git_ci_local_git.py`). Both are unblocked:
5.1 and 5.2 are landed and committed.

---

## Update — 22 October 2026 (ninth session): slices 5.3 and 5.4 landed

| Slice | Commit | What |
| --- | --- | --- |
| 5.3 | `524afed` | `GitCiAdapter` + the Phase 5 contract test (25 cases, real socket) |
| 5.4 | `524afed` | `LocalGitClient` — same interface, reads `.git` with pathlib, no subprocess |

### The second probe-found defect: the adapter's evidence was invisible to C-001

Slice 5.3's agent reported it, and the probe confirmed it. C-001 reads a pass/fail word out of
the evidence *text* (`\bpass(?:es|ed|ing)?\b|\bgreen\b` / `\bfail(?:s|ed|ing|ure)?\b|\bbroken\b|\bred\b`)
because the evidence model has no structured outcome field. GitHub says `success` and `failure`,
which match **neither**. So a successful run produced evidence no critic could read, and the
same-commit negative case (P10) only passed by accident of the job name carrying a pass marker.

**Fixed** in `_evidence_description` / `_workflow_description`: the host's own word is kept *and*
the translated outcome is added for exactly the two conclusions GitHub uses for pass/fail
(`success`→`passed`, `failure`→`failed`). Every other conclusion — `neutral`, `cancelled`,
`skipped`, `timed_out`, `action_required`, `stale` — is left verbatim with no pass/fail word
invented. Inventing one would be fabricating evidence, which is the one thing NYX exists to
avoid.

Proven end to end through the real ingestion path: C-001 now reads `success`→`passed`,
`failure`→`failed`, `neutral`→no outcome.

### Two defects found in total, both by probes, none by the slices' own tests

1. The adapter's evidence could not be stored at all (SPEC §5.4 vs §14) — found by
   `probe_git_ci_adapter.py`, fixed in ADR 0008.
2. The adapter's evidence was invisible to C-001 (GitHub vs critic vocabulary) — found by
   slice 5.3's agent and confirmed by the same probe.

**A slice's own tests assert the shape of what it returns. They do not assert that the rest of
the system can consume it.** Both defects live exactly at that seam.

### Gates (22 October 2026)

`uv run pytest` **637 passed, 1 skipped** · `uv run mypy` clean (128 files) · `uv run ruff check .`
clean · `uv run ruff format --check .` 209 files · `uv run alembic upgrade head` clean ·
boundary grep (G7) 13 passed · security allowlist (G1) 9 passed · `docs/openapi.json` unchanged ·
`tmp/probes/probe_git_ci_adapter.py` **34 checks, all green**.

### Next move

**Slice 5.5** — the fixture provider and `tests/integration/test_adapter_disablement.py`, which
is Phase 5's exit criterion: *adapters can be disabled independently and core NYX still works*.
It extends `nyx doctor`'s `optional_integrations` message to `registered: …; enabled: …` without
changing its pass/fail semantics (Phase 4 defect #7 is not to be repeated). After that, Phase 6.

---

## Update — 22 October 2026 (tenth session): Phase 5 is complete

**Slice 5.5 landed, and the doctor gap it could not close is closed.** Phase 5 is done:
six slices, one adapter, three providers, contract test, disablement test, ADR 0008.

| Slice | Commit | What |
| --- | --- | --- |
| 5.0 | `c0bc402` | `Adapter` protocol, registry, health, config, ADR 0008, boundary grep |
| 5.1 + 5.2 | `f652fcb` | `GitHubCiClient`, `GitCiConfig`, `normalize.py` |
| 5.3 + 5.4 | `524afed` | `GitCiAdapter` + contract test; `LocalGitClient` |
| 5.5 | `b3d5240` | `FixtureCiClient` + the disablement test |

### The exit criterion is met

`tests/integration/test_adapter_disablement.py` asserts all eight disablement properties,
including the one that matters most: **with the adapter enabled and its host unreachable,
`health()` is `DEGRADED` and `GET /api/v1/ready` is still `200`.** An optional adapter being down
never makes NYX unready. The health route was not modified to achieve that — the test proves it
does not have an adapter check.

### The gap slice 5.5 could not close, closed by the orchestrator

The plan's assertion 3 requires `nyx doctor` to say `registered: git-ci; enabled: false`. The
agent flagged that `cli/main.py` is orchestrator-only and outside its exclusive file list, and
refused to touch it — correct behaviour. `cli/main.py` now builds the adapter registry in the
doctor command and reports both halves:

```
default (installed, nothing configured):
  none enabled; git-ci registered but not constructible: repository is not set
repository set, still disabled:
  none enabled; registered: git-ci=disabled
enabled:
  configured: llm, git-ci; registered: git-ci=enabled
```

Phase 4 defect #7 was a doctor check that reported *configuration* as *availability*. The
inverse mistake is reporting only what is configured and hiding what is registered.

### Four defects found across Phase 5, none by the slices' own tests

| # | Defect | Found by |
| --- | --- | --- |
| 1 | Lifecycle test fixtures cited evidence no row backed | orchestrator, while closing scenario 24 |
| 2 | The adapter's evidence could not be stored (SPEC §5.4 vs §14) | `probe_git_ci_adapter.py` |
| 3 | The adapter's evidence was invisible to C-001 (GitHub vs critic vocabulary) | slice 5.3's agent, confirmed by the probe |
| 4 | `nyx doctor` hid a registered-but-disabled adapter | slice 5.5's agent |

**#2 and #3 are the same shape.** A slice's own tests assert the *shape* of what it returns;
they do not assert that the rest of the system can *consume* it. Both lived at that seam.

### Gates (22 October 2026)

`uv run pytest` **646 passed, 1 skipped** · `uv run mypy` clean (129 files) · `uv run ruff check .`
clean · `uv run ruff format --check .` 211 files · `uv run alembic upgrade head` clean ·
boundary grep (G7) + security allowlist (G1) 22 passed · `docs/openapi.json` unchanged ·
`tmp/probes/probe_git_ci_adapter.py` **34 checks, all green**.

### Honest limitations

- **The local-git provider reads refs and the reflog, not git objects** — no commit contents or
  diffs. Stated in the module docstring and ADR 0008.
- **`EvidenceItem` accepts unlinked evidence with a `relevance_note`** — an explicit SPEC §5.4
  deviation, recorded in ADR 0008, and reversible.
- **No new third-party dependency.** `httpx` plus the standard library. The SPEC §12 review that
  Phase 0 left open is closed by the plan's §1.
- **OpenTelemetry and the evaluation/policy/provenance adapters are not in Phase 5.**
- **The container image has still never been built** — Docker is not installed on this machine.

### Next move

**Phase 6 — optional model critics and calibration.** A pluggable LLM critic behind the same
interface as the deterministic ones, an evaluation corpus, and comparison against the
deterministic baseline. Re-validates scenarios 19 and 15. It needs its own dependency review
before anything is added.

---

## Update — 23 October 2026: NYX was run against itself, and it found a real bug

The question was "is NYX ready for daily use?". The gates said yes; running it on its own
repository said no. This is the difference between correctness and readiness.

### The self-audit

A fresh database, a real `uvicorn` subprocess, real HTTP, no `nyx.*` imports. NYX was registered
as a monitored system, facts verified by hand were ingested (HEAD commit, CI status, gate
results), and the critic pack was run against four claims. 20/20 checks passed, including the
negative controls: a claim with passing same-commit evidence produced **no** finding.

### Defect found — a disposition could make a finding permanently unreadable

Recording a disposition citing counterevidence wrote a row that violated the domain invariant
`supporting_evidence_ids ∪ counterevidence_ids ⊆ evidence_ids`. Every later read of that finding
returned HTTP 500 — permanently, because the row itself was invalid. The composer unions both
lists when it composes a finding; the lifecycle appended to `counterevidence_ids` only.

Fixed in `src/nyx/findings/lifecycle.py` by the same append-only, deduped union the composer uses.
Regression test: `tests/unit/test_disposition_counterevidence_regression.py` (2 tests, drives the
full HTTP scenario and asserts the **read-back** state, not the POST's 200).

Neither the 646-test suite nor the independent acceptance harness caught it, because neither read
a finding back after recording a disposition.

### Two CI failures, both of which had to be reasoned rather than run

- `uv sync --all-groups` syncs dependency **groups**, not optional **extras**, so `mcp` was absent
  and 9 test modules failed to collect. `--all-extras` is the fix.
- `aquasecurity/trivy-action@0.28.0` had no `v` prefix — but the prefixed tag was **still
  broken**: its composite action pins `aquasecurity/setup-trivy@v0.2.1`, which does not exist
  (tags are v0.2.6 / v0.3.0 / v0.3.1). Bumped to `@v0.36.0`, whose transitive refs are pinned by
  full SHA and both resolve. Verified against the GitHub API, not assumed.

### New: NYX watching an agent

`scripts/nyx_watch_agent.py` reads Hermes's own transcript store (`~/.hermes/state.db`) read-only,
files each assistant turn as a claim and each tool call plus result as evidence, then runs the
critic pack. On the orchestrator's last 17 turns it reported 2 claims with no tool call behind
them and 1 unsupported-certainty finding against the orchestrator's own wording.

This proves the wiring, not that the critics are calibrated for agent transcripts: C-001 is
shaped for completion claims naming a commit and correctly stays quiet. A dedicated
"unbacked claim" check is the natural next piece.

### Gates (23 October 2026)

`uv run pytest` **648 passed, 1 skipped** · `uv run mypy` clean (129 files) · `uv run ruff check .`
clean · `uv run ruff format --check .` 213 files · `uv run alembic upgrade head` clean on a fresh
database · `uv run nyx doctor` all checks passed · `scripts/acceptance_harness.py` ALL PASS / SKIP ·
the independent dispute-loop probe now returns 200 where it returned 500 before the fix.

### Next move

The remaining launch blocker is unchanged: **nothing runs unattended.** There is no scheduler or
worker, so ingestion and critic runs only happen when a human calls them. That is the difference
between a library and a service.

### Follow-up — the pipeline is green, and the scan found a base-image problem

CI had never been green, and the container scan had never run at all. Both are now fixed, and the
scan immediately earned its keep.

**CI**: `uv sync --all-groups` syncs dependency *groups*, not optional *extras*, so `mcp` was
absent and 9 test modules failed to collect. `--all-extras` is the fix. The dev venv already had
`mcp`, so a naive `uv sync --dry-run` proves nothing — the flag was verified against a clean
environment.

**Container scan**: `aquasecurity/trivy-action@0.28.0` had no `v` prefix. The prefixed `v0.28.0`
was *still* broken — its composite action pins `aquasecurity/setup-trivy@v0.2.1`, which does not
exist. Bumped to `@v0.36.0`, whose transitive refs are pinned by full SHA and both resolve.

**The scan then found real CVEs.** `python:3.12-slim` is Debian trixie, and the Dockerfile never
ran `apt` at all, so the image shipped whatever Debian published. The scan reported 35 HIGH
findings across **8 unique CVEs**. Debian's security tracker settles all eight: trixie is
**vulnerable with no fix**, and the fixes exist only in forky/sid (util-linux, systemd, ncurses,
acl and perl are each marked `<no-dsa>`). No forky variant of the `python` image is published, so
there is no patched base to move to.

An `apt-get upgrade` layer was added anyway — it clears whatever Debian *has* fixed, and it is the
correct hygiene — but the verified position is that it does not clear these eight. They are listed
in `.trivyignore`, one entry each, with the reason and the date to re-check written in the file.
`ignore-unfixed: false` is unchanged, so anything not listed still fails the build.

This is a base-image problem, not an NYX problem: the container runs as a non-root user
(uid 10001) and none of the vulnerable paths are NYX's. **The real remedy is a fixed base image**,
and the re-check condition is recorded in `.trivyignore`.

**Both workflows are green** as of `cd464bb`: CI 56s, Security scanning 1m05s. The scan log
confirms it did real work — image built, `.trivyignore` loaded, vulnerability DB downloaded,
vulnerability and secret scanning enabled, suppressions applied.

### Gates (23 October 2026, final)

`uv run pytest` **648 passed, 1 skipped** · `uv run mypy` clean (129 files) · `uv run ruff check .`
clean · `uv run ruff format --check .` 213 files · `uv run alembic upgrade head` clean on a fresh
database · `uv run nyx doctor` all checks passed · `scripts/acceptance_harness.py` ALL PASS / SKIP
· **CI green** · **Security scanning green** · the independent dispute-loop probe returns 200 where
it returned 500 before the fix.

---

## 24 October 2026 — caveat hardening closed, and the defects it exposed

The owner-approved plan `docs/implementation/caveats-plan.md` (H1–H5) is delivered. Every caveat it
named has a fix, and each fix is verified against the code rather than against the plan.

**What landed**

- **H1 licence** — Apache-2.0, recorded in `LICENSE`, `pyproject.toml`, README, and a
  dependency-licensing section in `docs/operations.md`.
- **H2a/H2b local-git objects** — `src/nyx/adapters/git_ci/objects.py` reads loose and packed
  commit/tree objects (OFS and REF deltas) with `zlib` only. No new dependency, no subprocess under
  `src/`. ADR 0011. Textual diffs remain out of scope and are still documented as such.
- **H3 external audit checkpoints** — `nyx audit checkpoint` and `nyx audit verify --against FILE`
  (ADR 0012). A fully recomputed rewrite no longer verifies. Every refusal is explicit.
- **H4 `nyx doctor --probe`** — opt-in reachability for the LLM endpoint and enabled adapters.
- **H5 seed demo + ADR 0010** — `nyx seed demo`, idempotent, and the missing ADR written.

**Four defects found while landing them, and fixed**

1. `display_effective_config` printed any URL setting verbatim. A password embedded in
   `NYX_LLM_BASE_URL` was therefore echoed by `nyx doctor`. Redaction now covers every URL.
2. `build_adapter_registry` constructed every adapter unconditionally, so `nyx doctor` and
   `doctor --probe` failed on a deployment with no Git/CI repository configured. A disabled adapter
   that cannot be built is now described, not raised.
3. `httpx` logs every request at INFO, which put a line on stdout in front of the JSON a caller was
   parsing. A CLI whose stdout is one JSON document now suppresses the HTTP client's and alembic's
   loggers for the duration of a command.
4. The seed exposed a replay mislabelled as `created`. A manifest whose id already matches the stored
   row *is* the replay case, and the gateway reported it as a write.

**The run before this one** (Phase 6, slices 6.1–6.5) is also in this tree: the `nyx.llm` provider
seam, opt-in c011 registration, provenance and privacy-gated I/O persistence, model-path
injection/provider-failure suites, and a fixture evaluation corpus and runner.

**Still open**

- **The golden set is drafted but not completed.** `docs/implementation/golden-set-run.md` is the
  record. Two cases were drafted from `docs/SPEC.md`, both seed and score correctly, and **none is
  approved** — the review step is a human gate by design (ADR 0013) and no review has happened.
  Fifteen of the seventeen labelled clauses were refused by dmr-X's prompt-injection guardrail
  with HTTP 400, so the set cannot be grown against this provider today.
- **CI is green on the pushed commit.** `caad1f4` passed both `CI` and `Security scanning`
  (`gh run list`, 26 October 2026). The eleven commits before it had never been through a
  pipeline; they have now.
