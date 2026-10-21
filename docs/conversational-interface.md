# The conversational interface

**Status:** working, Phase 7 slices 6.2–6.4 landed. **Rule that governs all of it:**
*the language model is the interface, never the judge.*

```text
nyx chat  ──►  ChatAgent  ──►  LLM provider  (dmr-X, Ollama, any OpenAI/Anthropic-compatible)
   ▲              │
   │              ▼
   └──────  NyxClient  ──HTTP──►  NYX core (unchanged)
```

The conversational layer lives in `src/nyx/conversational/` as a **separate, independent unit**.
It imports nothing from `nyx.critics`, `nyx.findings`, `nyx.lifecycle`, `nyx.policies`, or
`nyx.storage`, and reaches the core only over HTTP. Its dependencies are an extra:
`pip install nyx[conversational]`.

## What it can do

```bash
nyx chat ask "What findings does NYX have?" --json
nyx chat shell                      # interactive
```

The model can **query** anything, **explain** what it finds, **challenge** a finding by filing a
rebuttal, and **ask** for verification. It can do those things because they are the same powers a
human has through the API.

## What it cannot do

It **cannot** create a finding, record a disposition, or change a lifecycle state. Not "it is
instructed not to" — those operations **do not exist in its tool surface**. The 12 registered
tools are read-mostly, plus three writes: file a rebuttal, request verification, run queued
verification.

The safety property is enforced structurally, and tested from three directions
(`tests/unit/test_conversational_agent.py`):

1. `tool_specs()` offers no judge-capable tool.
2. `assert_no_judge_tools()` refuses to start if one appears.
3. A model that calls one gets a `TOOL_FORBIDDEN` result naming the refusal.

**Do not add a tool without asking whether a human could do it by hand.**

## Configuration

Everything comes from the environment / `.env`. Write your own key there; never paste one into chat.

| Variable | Default | Meaning |
| --- | --- | --- |
| `NYX_LLM_ENABLED` | `false` | Turns the chat shell on. |
| `NYX_LLM_MODEL` | — | **Required** when enabled. The shell will not guess a model. |
| `NYX_LLM_PROTOCOL` | `openai` | `openai` (`/v1/chat/completions`) or `anthropic` (`/v1/messages`). |
| `NYX_LLM_BASE_URL` | `http://127.0.0.1:3000` | The provider or gateway origin. |
| `NYX_LLM_API_KEY` | — | Sent as the protocol's own auth header. Optional when a gateway holds the key. |
| `NYX_LLM_TIMEOUT_SECONDS` | `120` | Per model call. |
| `NYX_LLM_MAX_RETRIES` | `0` | Retried on `unavailable` only — never on a refusal. |
| `NYX_LLM_MAX_TOOL_ROUNDS` | `8` | A runaway loop stops instead of running forever. |

**dmr-X is the design default**: point `NYX_LLM_BASE_URL` at its gateway and NYX needs no key at
all, because dmr-X holds the credentials. It is a separate project consumed over HTTP — its code is
never vendored here. For direct mode, set `base_url` + `api_key` + `model` for any provider you hold.

### A credential must never go in `base_url`

`httpx` turns `https://user:pass@host` into a Basic auth header on its own, so a password embedded
in `base_url` would go out on the wire in a header nothing here set. The adapter **refuses** such a
URL with `INVALID_REQUEST` and points at `NYX_LLM_API_KEY` instead. Silently dropping the password
would send an unauthenticated request that fails for a reason the user did not ask for.

## Prompt injection

Everything NYX returns — a finding's summary, a rebuttal's argument, an evidence description — is
**untrusted data about other systems**. It is passed to the model as tool results, never merged
into the system prompt, and never treated as instructions. The server instructions say so
explicitly; the system prompt repeats it.

## Known limitations

- **Local reasoning models spend their budget thinking.** Ollama returns a `reasoning` field the
  adapter does not parse, so a model that thinks for its whole budget returns empty content. With
  `qwen3.5:0.8b` and a small store, a full chat turn takes **30–90 seconds** and can exceed a short
  timeout. Raise `NYX_LLM_TIMEOUT_SECONDS` or use a faster non-reasoning model.
  **Measured 21 October 2026:** a two-round turn against `qwen3.5:0.8b` took **2m07s**, so the
  default `NYX_LLM_TIMEOUT_SECONDS=120` is *just* too short for this model — a single call can run
  past it, and the turn fails as `the model is unavailable (timeout)` rather than returning the
  answer it had already computed. With `NYX_LLM_TIMEOUT_SECONDS=300` the same question succeeded.
  A reasoning model needs a generous per-call budget, not a short one.
- **A model can still embellish.** Observed: asked about a finding, the model appended
  "recommended actions" that NYX never returned. Every substantive claim cited a real id, but the
  surrounding prose was invented. The prompt forbids this; it is not yet structurally prevented.
  Treat the model's prose as a summary to verify, not as NYX's output.
- **No authentication yet.** Phase 4. Until then the API is loopback-only.

## Verification

```bash
uv run python scripts/probe_mcp_live.py        # real stdio session against a live NYX
uv run python scripts/probe_mcp_roundtrip.py   # real finding round-trip + rebuttal
uv run python scripts/probe_critic_pack.py      # whole critic pack, incl. C-010
```

See `scripts/README-live-probes.md`. These probes found five real defects that the unit tests had
passed over — including one in the deterministic core.
