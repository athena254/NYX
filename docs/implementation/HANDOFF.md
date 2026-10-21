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

