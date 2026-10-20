# Conversational interface plan — NYX

**Source:** user request, 20 October 2026. **Status:** researched, not started. **Placement:** in the NYX repo as a separate independent unit. **Default backend:** dmr-X inference gateway (called over HTTP, never vendored).
**Scope:** a standalone conversational front-end for NYX, with the CLI as the machine interface for
other systems.

## 1. What this is

A chat layer over NYX. You talk; NYX judges.

### Placement (decided)

The conversational layer lives **inside the NYX repository**, but as a **separate, independent
unit** — never merged into the deterministic core. Concretely: its own package directory, its own
dependency boundary, and no import from the deterministic modules beyond the public API/CLI surface.

"Keep independent projects independent" cuts two ways, and both are honoured here:

- **Within NYX:** the deterministic core (`critics`, `findings`, `lifecycle`, `policies`, `storage`)
  and the conversational shell do not share code. The shell talks to the core through the API and CLI,
  never by importing its internals.
- **Between projects:** dmr-X is called over HTTP as a service. Its code is not vendored into NYX, and
  NYX's code is not copied into dmr-X. Each project stays independently buildable and deployable.

The user asked for something that feels like talking to an assistant. The design that satisfies that
without destroying NYX is a **chatty critic**, not a general assistant:

- You ask "what's wrong with Ghost Factory right now?", "show me the finding about commit abc123",
  "challenge it, here's my evidence".
- NYX answers from its own store, explains its reasoning in plain language, and files things on your
  behalf.
- NYX never becomes a general-purpose assistant that goes and does things. It remains the sceptic.

## 2. The one rule that makes or breaks this

> **The language model is the interface, not the judge.**

NYX's entire value is that its verdicts are deterministic and evidence-linked — reproducible, not
vibes. SPEC §3.2 is explicit: *model output is assessment, never deterministic verification.*

So the split is absolute:

| Layer | Job | Must never do |
| --- | --- | --- |
| LLM (conversational) | Understand the request, query NYX, explain the answer | Generate a finding, a disposition, or a verdict |
| NYX core | Judge, persist, enforce | Be bypassed by the conversational layer |

**The conversational layer has exactly the powers a human has through the API, and no more.** It can
query anything, file a rebuttal, and request verification. It cannot create a finding, record a
disposition, or change a lifecycle state — those paths do not exist in its tool surface, and NYX
rejects them anyway.

A chat that *cannot* hallucinate a verdict is the point, not a limitation.

## 3. What already exists (researched, not assumed)

### 3.1 In the NYX repository

| Asset | State |
| --- | --- |
| HTTP API | ~25 endpoints under `/api/v1`, incl. the full dispute protocol. Working. |
| CLI | 15 commands, **all with `--json`**, 21 explicit exit-code paths, **no interactive prompts**. |
| Machine-readiness | Already good for local scripting. |

**One important limitation:** the CLI opens the **database directly** via `Settings()` — it does not
call the HTTP API. Every command calls `create_engine_from_settings`. That is fine for a local script
but means a remote system cannot use the CLI without database access.

Implication for "used by other systems": the HTTP API and an MCP server are the right interfaces. The
CLI stays the local-scripting surface. This is a real design decision, recorded rather than assumed.

### 3.2 Elsewhere on this machine (reusable, do not rebuild)

| Asset | Location | Relevance |
| --- | --- | --- |
| Multi-provider LLM layer (Python) | `C:\Users\PC\The-Agency\src\agency\llm\` | `openai.py`, `anthropic.py`, `openrouter.py`, `ollama.py`; `ProviderKind` enum; `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` / `OLLAMA_HOST`. **Proven pattern to port.** |
| Telegram bot stack | `C:\Users\PC\The-Agency\src\agency\telegram\` | `adapter.py`, `handler.py`, `server.py`, `rate_limit.py`, `budget_store.py`, `profile_store.py`. Polling + webhook, rate limiting, budgets. **Proven pattern to port.** |
| Multi-provider LLM config (TypeScript) | `D:\chimera\repo\config.multi-model.example.yaml` | `openai-compatible` provider with `${ENV}` key resolution and writer/reviewer/challenger roles. Confirms the config shape. |
| **dmr-X inference gateway** | `C:\Users\PC\dmr-X\apps\gateway` | **The inference endpoint.** Serves `POST /v1/chat/completions` (OpenAI-compatible), `POST /v1/messages` (Anthropic-native), and `/v1/models`. Holds the API keys; NYX never sees them. **Called over HTTP, never vendored.** |
| Hermes MCP client support | `AppData\Local\hermes\config.yaml` (`mcp_servers:` block) | Hermes can host a local MCP server and expose its tools. **Zero new infrastructure for the conversational path.** |

**Nothing here needs to be rebuilt.** The LLM provider layer and the Telegram bot stack are both
proven in production on this machine. The work is wiring, not invention.

## 4. Cloud LLM providers — researched, with real numbers

Prices are per million tokens, input/output, as published 2026. Treat as indicative; verify before
committing to a spend level.

| Option | Price (in / out) | Notes |
| --- | --- | --- |
| **Groq — GPT-OSS 120B** | $0.15 / $0.60 | ~500 tok/s. Reliable OpenAI-compatible tool calls. **Free tier: all models, 30 req/min.** |
| **OpenRouter — `:exacto`** | passes through provider price | One key, 500+ models, OpenAI-compatible, automatic failover. `:exacto` routes toward tool-calling reliability. 5.5% fee on credit top-ups. Free tier ~25 models, 50 req/day. |
| **DeepInfra — DeepSeek V3** | $0.14 / $0.28 | Cheapest per-token. |
| **DeepInfra — Llama 3.3 70B** | $0.23 / $0.40 | |
| **Anthropic — Claude Haiku 5.5** | $0.10 / $0.50 | Cheapest first-party. |
| **OpenAI — GPT-6 Luna** | $0.10 / $0.50 | Cheapest first-party. |
| **Anthropic — Claude Sonnet 5** | $2 / $10 | Best reasoning; 5× the cost. |
| **OpenAI — GPT-6 Astra** | $10 / $50 | |

**The decision that matters is not the model, it is the interface.** Every option above is reachable
through an **OpenAI-compatible** `base_url` + `api_key` + `model` triple. The-Agency already does
exactly this (`providers/openai.py` takes `base_url` and `api_key` overrides). Build that triple and
the provider becomes a config change, not a code change.

**Two-tier recommendation.** Use **dmr-X as the default backend** — it holds the keys, routes across
57+ providers, and normalises both wire protocols behind one endpoint, so NYX needs no key of its own.
For direct mode (bypassing dmr-X), **OpenRouter** is the best single choice: one key covers every
provider, `:exacto` routes toward tool-calling reliability, and it is already proven in Chimera's
config on this machine. **Groq** is the fast/free alternative for high-volume local use.

**Cost estimate.** A conversational session issues roughly 5–15 tool calls. At OpenRouter's blended
agent rates (~$0.23–$0.90 in / $0.40–$0.90 out per M), a session costs well under one cent. A user
having 50 conversations a day is roughly $0.05–$0.50/day. This is not a cost concern at this scale.

## 5. Architecture

Three layers. Each has one job and a clean boundary.

```text
┌─────────────────────────────────────────────┐
│  Layer 3 — Conversational front-end         │
│  interactive CLI chat, then Telegram bot     │
│  LLM decides WHAT to ask; never judges       │
└───────────────┬─────────────────────────────┘
                │ tool calls (MCP)
┌───────────────▼─────────────────────────────┐
│  Layer 2 — NYX MCP server  (in NYX repo)    │
│  NYX's operations exposed as tools           │
│  the interface other systems consume         │
└───────────────┬─────────────────────────────┘
                │ HTTP (existing API)
┌───────────────▼─────────────────────────────┐
│  Layer 1 — NYX core  (unchanged)             │
│  deterministic critics, lifecycle, disputes  │
└─────────────────────────────────────────────┘

                ┌─────────────────────────────────┐
                │  dmr-X inference gateway        │
                │  separate project, separate repo │
                │  holds the API keys              │
                │  /v1/chat/completions (OpenAI)   │
                │  /v1/messages (Anthropic)        │
                └─────────────────────────────────┘
```

The LLM provider is a **separate process in a separate project**. NYX's conversational layer calls it
over HTTP; it does not import it, and dmr-X does not depend on NYX.

**Why MCP for layer 2.** The user asked for two things at once: standalone, *and* usable by other
systems. MCP is the standard interface for exactly that, and Hermes already hosts local MCP servers —
so the conversational path can be exercised today with zero new infrastructure. The CLI remains the
local-scripting surface; MCP is the machine-to-machine surface.

**Why the front-end is separate.** NYX core must stay a clean deterministic service. A chat layer
that can be pointed at a different LLM tomorrow does not belong inside it. Separate project, clear
boundary, NYX unchanged.

## 6. Work items, in dependency order

### 6.1 Harden the CLI for other systems

The CLI already has `--json` on every command and no interactive prompts. Remaining work:

- [ ] A `--help` contract test asserting every command exposes `--json` and documents its exit codes.
- [ ] Document the exit-code table (success, not found, validation, capability denied, refused
      transition) so a calling system can branch on it.
- [ ] Confirm no command writes to stdout anything but the JSON document when `--json` is set.
- [ ] Decide and document the CLI's stance: local scripting (direct DB) vs remote (HTTP). Recommendation:
      keep direct-DB for local, and point remote systems at the HTTP API or MCP.

### 6.2 NYX MCP server

- [ ] A local MCP server (stdio transport) exposing NYX's read and write operations as tools.
- [ ] **Read tools**: list/get findings, list critics, get history, list rebuttals, list verification
      jobs, list policy versions, list systems/claims/evidence.
- [ ] **Write tools**: submit rebuttal, request verification, run queued verification.
- [ ] **Explicitly absent**: create finding, record disposition, change lifecycle state. The tool
      surface does not offer them.
- [ ] Structured errors surfaced as tool results, never as prose that could be mistaken for a verdict.
- [ ] A test proving an unauthorised write is refused by NYX core, not by the server.

### 6.3 LLM provider adapter — two protocols, one abstraction

NYX must accept **any Anthropic or OpenAI-compatible LLM**. The adapter therefore supports two wire
protocols behind one interface:

| Protocol | Endpoint | Used by |
| --- | --- | --- |
| OpenAI-compatible | `POST {base_url}/v1/chat/completions` | OpenAI, OpenRouter, Groq, DeepInfra, Together, Ollama, vLLM, and dmr-X |
| Anthropic Messages | `POST {base_url}/v1/messages` | Anthropic direct, and dmr-X |

- [ ] One adapter, two protocol backends, selected by config (`NYX_LLM_PROTOCOL=openai|anthropic`).
- [ ] **dmr-X is the default backend.** Point at its gateway and NYX needs **no API key at all** —
      dmr-X holds the credentials. NYX sends `base_url` only.
- [ ] Direct mode also supported: `base_url` + `api_key` + `model` for any provider the user holds.
- [ ] Tool calling on both protocols (OpenAI `tools[]`; Anthropic `tools[]` with `tool_use` blocks).
- [ ] **Secrets read from environment/`.env` only.** Never logged, never echoed, never committed.
      Follow `nyx.config`'s `SecretStr` + `_looks_secret` redaction pattern.
- [ ] Timeout, retry on idempotent calls only, and a hard failure mode that says "the model is
      unavailable" rather than degrading into guessing.

### 6.3a dmr-X gateway — current state and what it needs

Researched 20 October 2026. **dmr-X is not currently running.** Verified:

- `.env` sets `PORT=3000`; nothing is listening on 3000.
- `DMRX_MCP_PORT=47114` with `DMRX_MCP_TRANSPORT=stdio`; nothing on 47114.
- PM2, the supervisor declared in `ecosystem.config.cjs`, is not installed.
- No build output exists (`apps/gateway/dist` absent) — the project is source-only.
- Companion ports are also down: 3100 (MCP/A2A sidecar), 7860 (G0DM0D3), 8011 (needle-router), 47113.

To use it, dmr-X must be built and started:

```bash
cd C:\Users\PC\dmr-X
bun install
bun run build          # or: pm2 start ecosystem.config.cjs
bun run start          # gateway on :3000
```

**This is a prerequisite for exercising 6.3 against dmr-X, and it is the user's call** — starting a
large TypeScript monorepo build is a significant operation on this machine (RAM is chronically tight).
Until it runs, the adapter can still be developed and tested against a direct provider endpoint.

### 6.3b Why dmr-X as the default is a good design

dmr-X is a **universal routing gateway**: it already holds 57+ provider adapters and normalises them
behind OpenAI-compatible and Anthropic-native endpoints. That means:

- NYX never stores a provider key. dmr-X owns credential rotation, quota, and failover.
- Switching models is a dmr-X routing decision, not an NYX redeploy.
- NYX stays a clean deterministic service that happens to have a chat front-end.

The cost is one extra network hop and a dependency on dmr-X being up. Both are recorded in the risks.

### 6.4 Conversational front-end

- [ ] An interactive CLI chat: `nyx chat` — the fastest path to a usable product.
- [ ] A Telegram bot, porting The-Agency's proven stack (handler, rate limiting, budget, error
      privacy). Reuse its patterns; do not fork its code.
- [ ] A system prompt that pins the persona: a sceptical critic that only asserts what its tools
      returned, cites finding IDs, and never invents a verdict.
- [ ] Session memory: what was discussed, which findings were touched.
- [ ] Every substantive answer cites the finding/disposition ID it came from, so the user can verify.

### 6.5 Tests

- [ ] MCP server: every tool returns a typed result; an unauthorised write is refused.
- [ ] Provider adapter: timeout, invalid key, and provider-down paths all fail closed.
- [ ] Front-end: a test proving the LLM **cannot** produce a finding or disposition — the tool surface
      does not expose them, so the attempt fails.
- [ ] Prompt-injection: instructions embedded in a finding's summary or a rebuttal's argument must not
      change what the bot does (SPEC §17.2 scenario 15 pattern).
- [ ] CLI contract test for `--json` and exit codes.

### 6.6 Documentation

- [ ] `docs/conversational-interface.md` — what the chat can and cannot do.
- [ ] Provider setup: which env vars, and the instruction to write the key into `.env` locally.
- [ ] Update `roadmap.md` with this phase.

## 7. Acceptance criteria

1. `nyx chat` starts, and a question about stored findings returns an answer citing real finding IDs.
2. A user can file a rebuttal and request verification entirely by chatting.
3. **No code path exists** by which the conversational layer can create a finding or record a
   disposition. Proven by test, not by inspection.
4. Pointing the provider at a different `base_url`/`model` requires a config change only.
5. **Both protocols work**: the same tool-calling request succeeds against an OpenAI-compatible
   endpoint (`/v1/chat/completions`) and an Anthropic endpoint (`/v1/messages`).
6. An unavailable provider produces a clear failure, never a fabricated answer.
7. Every CLI command's `--json` output and exit codes are contract-tested.
8. The Telegram bot (if built) rate-limits and never leaks another user's data.

## 8. Risks and open questions

- **Which provider does the user's key belong to?** If dmr-X is the backend this does not matter —
  dmr-X holds the key and NYX needs none. It only matters for direct mode.
- **dmr-X must be running.** Verified not running as of 20 October 2026 (see 6.3a). The adapter can be
  built and tested against a direct endpoint meanwhile, but the default path needs the gateway up.
- **dmr-X becomes a runtime dependency.** If it is down, the chat is down even though NYX core is
  healthy. Mitigation: a clear "inference unavailable" failure rather than a silent fallback to
  guessing, and the option to point NYX at a direct provider.

### Placement decision (recorded)

The conversational layer lives **in the NYX repository as a separate independent unit**, not a
separate repository. Reasons:

- The user wants one project, and NYX is the natural home for its own interface.
- Independence is preserved by construction: a separate package with its own dependency boundary,
  talking to the deterministic core only through the public API and CLI.
- Vendoring dmr-X code was rejected for the same reason — dmr-X is called as a service, so both
  projects stay independently buildable and deployable.
- **Cost control.** A runaway agent loop could spend real money. The Telegram stack already has a
  budget store; port it, and add a per-session tool-call cap.
- **Prompt injection is the main safety risk.** All retrieved NYX content is untrusted data. A
  finding's summary or a rebuttal's argument must never be able to instruct the bot.
- **The LLM must not become a silent second judge.** If it ever starts summarising a finding as
  "actually fine", the safety property is gone. Answers must cite IDs, not paraphrase verdicts.

## 9. Deliberately out of scope

- NYX as a general-purpose assistant that performs actions. Contradicts SPEC §2 ("NYX is the critic,
  not the unchecked ruler") and ADR 0004 (no external action in the MVP).
- LLM-generated findings or dispositions. This would destroy the deterministic guarantee.
- Rebuilding the LLM provider layer or the Telegram stack. Both already exist and are proven.
- Vendoring dmr-X code into NYX. dmr-X is a separate project consumed over HTTP.
- Merging the conversational shell into the deterministic core. They stay independent units.
