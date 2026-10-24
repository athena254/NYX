# Phase 6 plan — Optional model critics and calibration

**Source:** `docs/SPEC.md` §13, §15, §16, §17.1, §17.2 (scenarios 15, 19), §19 (Phase 6), §20
step 16; ADR 0005 (evidence is not proof), ADR 0006 (untrusted content), ADR 0009 (unattended
operation); `docs/implementation/roadmap.md` §Phase 6;
`docs/implementation/HANDOFF.md` (23 October 2026 update); `docs/implementation/task-checklist.md`
step 16.
**Owner of this plan:** the orchestrating agent. **Status:** accepted by the project owner,
24 October 2026.
**Phase 6 exit criterion (SPEC §19):** *schema validation, evidence-ID validation, prompt-injection
tests, provider-failure tests, and documented evaluation results pass.*

**Last verified against the code:** 24 October 2026, branch `master`, HEAD `644a44c`, working tree
clean. `uv run pytest` → **648 passed, 1 skipped**; `uv run mypy` clean (129 files);
`uv run ruff check .` clean; `uv run ruff format --check .` clean.

Every claim about existing code below was read, not inferred from a filename.

---

## 0. Ground rules this phase does not restate lightly

- **The model is the interface, never the judge** (Phase 7 rule). A model critic may *propose* a
  finding; it never records a disposition, never changes a lifecycle state, and never grants a
  capability.
- **Model output is assessment, never verification.** Model-emitted confidence is rejected by the
  output schema, not reinterpreted.
- **Evidence IDs are validated against the context.** An assessment citing evidence NYX has not
  linked is `INCONCLUSIVE`, never a finding.
- **Fail closed.** No transport, timeout, transport error, invalid JSON, schema violation, refusal,
  or unlinked evidence ID composes no finding and changes no stored state.
- **Secrets from the environment / `.env` only.** `base_url` must not embed credentials (already
  enforced, `src/nyx/conversational/llm.py:226-245`).
- **Tests ship with the slice**, and each slice is gated by the project's own commands plus a probe
  written outside the project's test helpers.

---

## 1. What already exists (countercheck before proposing anything)

Phase 6 is smaller than its name suggests because two earlier phases built most of the machinery:

| Artifact | Where | State |
| --- | --- | --- |
| Failure-safe LLM critic (c011) | `src/nyx/critics/llm.py` | Interface, strict `OUTPUT_SCHEMA`, `_LlmAssessment`, evidence-ID validation, refusal handling, finding preconditions, `build_model_prompt` fencing. Docstring: "deliberately contains **no provider transport**: a real adapter is Phase 6 work." |
| Critic failure tests (stub path) | `tests/unit/test_llm_critic_failure.py` | Scenario 19 for the stub only. |
| Provider transports | `src/nyx/conversational/llm.py` | OpenAI-compatible `/v1/chat/completions` and Anthropic `/v1/messages`; `LLMConfig`; `LLMError` codes; credential scrubbing; live-proven against dmr-X (HANDOFF, fourth session). |
| LLM settings | `src/nyx/config.py:60-73` | `llm_enabled`, provider/model/protocol/base_url/api_key/timeout/retries/max_tool_rounds. |
| Critic-run persistence | `src/nyx/storage/tables.py:213-225` | `critic_runs` carries `critic_id`, `critic_version`, `result`, `input_refs`, `input_hashes`, timestamps. **No provider/model/prompt-version/usage/prompt-body columns.** |
| Critic registration seam | `src/nyx/critics/__init__.py` | Explicit registry; `register_storage_backed()` is the precedent for a factory that takes a caller-owned dependency. |
| Injection harness | `tests/security/test_prompt_injection.py` | Fence cannot be closed; policy resolution reads only the registered rule set. Model-critic path not covered. |

**Missing for Phase 6:** a synchronous transport bridge for the critic, registration/wiring,
provider/model/prompt-version/usage persistence, prompt-body privacy handling, model-path injection
tests, live provider-failure tests, the evaluation corpus, the baseline comparison, and documented
thresholds.

---

## 2. Dependency / license review (SPEC §12)

**Verdict: Phase 6 needs ZERO new third-party runtime dependencies.**

- `httpx` (0.28.1, BSD-3-Clause) is already a runtime dependency and is already the project's HTTP
  client. It supplies `httpx.Client` for the synchronous transport.
- `jsonschema` (4.26.0, MIT) is already a runtime dependency (`pyproject.toml:19`) and is what
  validates c011 output today.
- The evaluation corpus is JSON Lines on disk; the runner uses the standard library only.
- The next free ADR numbers are 0010 (see caveats plan — the reference exists but the file is
  missing) and 0011+.

No package is added to `pyproject.toml` by any slice in this plan.

---

## 3. Design decisions

### 3.1 One provider seam, two call styles

`src/nyx/conversational/llm.py` moves to `src/nyx/llm/provider.py`. The old module remains as a
re-export shim so no call site changes behaviour (the CLI imports it, `src/nyx/cli/main.py:36-39`).
The provider module is refactored minimally so request assembly and response parsing are callable
from both an async client and a sync transport:

- `encode_openai(...)/encode_anthropic(...)` and the two decoders become module-level functions;
  the existing async clients keep their public `chat()` behaviour.
- `src/nyx/critics/llm_transport.py` adds `SyncCriticTransport(config: LLMConfig)` implementing
  `LlmTransport.complete(prompt, schema, timeout_seconds) -> str` with `httpx.Client`.
- **Drift control:** `tests/contract/test_llm_provider_parity.py` stands up a local socket server,
  drives the async client and the sync transport at the same config, and asserts identical
  method/path/headers/JSON body (minus credential headers where absent) and identical parsed
  content.

The critic gains a `timeout_seconds` constructor argument (default 30s) instead of the hardcoded
module constant, so the transport and the critic agree on one deadline.

### 3.2 Privacy mode and persistence

`NYX_LLM_LOG_PROMPTS=false` by default: the prompt and raw response are **not stored**. When
`true`, both are passed through the existing ingestion redaction pipeline
(`src/nyx/ingestion/redaction.py`) before being written to a nullable `llm_io` JSON column.

A one-migration change adds nullable columns to `critic_runs`:
`model_provider`, `model_name`, `prompt_version`, `usage` (JSON: input/output tokens), `llm_io`
(JSON, only when `NYX_LLM_LOG_PROMPTS=true`). No append-only rule is touched; critic runs are
already write-once.

`NYX_LLM_CRITIC_ENABLED=false` by default. Enabling it without `NYX_LLM_MODEL` is a
`ValidationError`, exactly as `NYX_LLM_ENABLED` already behaves. c011 is registered by a new
`register_llm_backed()` only when enabled; registration is wired in the two orchestrator-owned
files (`src/nyx/api/app.py`, `src/nyx/cli/main.py`) so a delegated slice cannot invent a second
registration path.

### 3.3 Evaluation methodology

- Corpus: `evals/llm-critic/corpus.jsonl`, one labelled case per line with the evidence bundle,
  the claim, and the expected outcome family (`finding` / `none` / `inconclusive`).
- Sources, recorded per case: synthetic cases derived from existing acceptance scenarios, the
  consented anonymised examples from the 23 October self-audit, and existing fixtures under
  `tests/fixtures/`.
- Baseline: the deterministic pack on the same corpus. Deterministic outcomes are exact and
  regression-tested in the corpus test.
- Model metrics: false positives, false negatives, unsupported assertions, latency, token counts.
  No price table is invented; cost is reported as tokens.
- Thresholds: `evals/llm-critic/thresholds.json` is produced by a recorded live run and then
  treated as the regression floor. Until the live run lands, the file records `"pending"` and the
  unit test measures the deterministic baseline only — honest rather than fabricated.
- Scenario 19 (live path) and scenario 15 (model path) are re-validated by test, not by prose.

### 3.4 What the model may still do (recorded, not hidden)

A model can embellish prose or propose a weak finding. The structural controls are: no tool
surface, no lifecycle authority, evidence-ID validation, forced `requires_human_review` on every
model-composed finding, and the C-010 self-consistency gate on confirmation. "Cannot embellish at
all" is not claimed.

---

## 4. Slices

`src/nyx/api/app.py` and `src/nyx/cli/main.py` are **orchestrator-only**. Every delegation below
lists exclusive file ownership; a delegated slice must not touch a file it does not own.

| Slice | Deliverable | Files (owner) | Tests | Gates |
| --- | --- | --- | --- | --- |
| **6.1** Provider seam | Move provider to `nyx/llm/`, keep shim, split encode/decode, add `SyncCriticTransport`, critic timeout argument | `src/nyx/llm/*`, `src/nyx/conversational/llm.py` (shim), `src/nyx/critics/llm_transport.py`, `src/nyx/critics/llm.py` (orchestrator) | `tests/unit/test_llm_provider.py` (moved/extended), `tests/contract/test_llm_provider_parity.py`, `tests/unit/test_llm_critic_transport.py` | all four + parity test over a real socket |
| **6.2** Wiring + storage | Settings, migration, `register_llm_backed()`, persistence of provider/model/prompt version/usage/privacy-gated bodies | `src/nyx/config.py`, `src/nyx/critics/__init__.py`, `migrations/versions/*`, `src/nyx/critics/registry.py` (only if a keyword is needed), `src/nyx/api/app.py`, `src/nyx/cli/main.py` (orchestrator) | `tests/unit/test_llm_critic_registration.py`, `tests/integration/test_llm_critic_run_persistence.py`, migration test | all four + `alembic upgrade head` on a fresh DB |
| **6.3** Security + live failure | Model-path injection suite; fake-provider socket tests for timeout/invalid JSON/refusal/unknown ID/happy path; scenario 15 and 19 re-validation; live probe | `tests/security/test_llm_critic_injection.py`, `tests/integration/test_llm_critic_provider_failures.py`, `scripts/probe_llm_critic.py` | as listed | probe green against a real migrated DB |
| **6.4** Evaluation | Corpus, runner, deterministic baseline assertions, thresholds file, evaluation write-up | `evals/llm-critic/*`, `scripts/eval_llm_critic.py`, `docs/implementation/phase-6-evaluation.md` | `tests/unit/test_eval_corpus.py`, `tests/unit/test_eval_thresholds.py` | runner output reported; live run optional and owner-gated |
| **6.5** Status sync | Roadmap, acceptance table, task checklist, README, HANDOFF | docs (orchestrator) | — | doc diff reviewed against actual test output |

Sequential dependency: 6.1 → 6.2 → 6.3 → 6.4 → 6.5.

---

## 5. Risks and open items

| # | Risk | Mitigation |
| --- | --- | --- |
| R1 | Payload drift between the sync transport and the async client | The parity contract test is the gate; failing it fails the slice |
| R2 | `asyncio` bridging from the API's thread pool | Avoided entirely: the transport is synchronous `httpx.Client`; no event loop is entered |
| R3 | A model critic silently widening scope | c011 has no tool surface, no capability checker access, and composes only through the existing composer |
| R4 | Live provider instability (dmr-X free upstreams observed `429`/`502`) | Live runs are opt-in and recorded; provider failures assert fail-closed behaviour, not model quality |
| R5 | Evaluation corpus too small to mean anything | Corpus size and composition are stated in the evaluation write-up; thresholds are a regression floor, not a calibration claim |
| R6 | Prompt/response storage leaking secrets when logging is enabled | Redaction runs before the write; a test injects a secret and asserts it is absent from the stored row |

---

## 6. Definition of done for the phase

- c011 runs through the same registry/composer/lifecycle path as the deterministic critics, only
  when explicitly enabled, and is absent otherwise.
- Provider/model/prompt-template version are persisted with every run; token usage too.
- Prompt bodies are absent unless logging is enabled, and redacted when it is.
- Scenario 19 passes with the real provider transport over a socket, and scenario 15 passes with
  model-generated risk paths fenced.
- The evaluation runner produces a documented report against the deterministic baseline.
- `uv run pytest`, `uv run mypy`, `uv run ruff check .`, `uv run ruff format --check .`, and
  `uv run alembic upgrade head` pass, with real output reported.
- The live run against dmr-X is performed only with the owner's go-ahead and its result recorded;
  until then the docs say the evaluated path is fixture-transport only.
