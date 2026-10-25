# NYX discovery — ground-truth verification and behavioural QA

**Status:** Phase 0 of the ground-truth upgrade. No behaviour changed; no code written. This
document answers the Phase 0 questions and ends with the implementation plan.
**Date:** 25 October 2026.
**Method:** read the repository at `D:\NYX` (branch `master`, head `676b8d9`) and ran its own
gates. Every NYX claim below is verified against the code, not assumed from the plan. Where a
claim is a judgement it says so.

**Baseline measured before any change:**

```
uv run pytest          819 passed, 1 skipped in 114.03s
uv run mypy            Success: no issues found in 146 source files (strict)
uv run ruff check .    All checks passed!
uv run ruff format .   252 files already formatted
uv run alembic upgrade head   clean on a fresh database
```

That baseline is the number this upgrade must not reduce, and it is the reference for the
"existing tests pass unchanged" acceptance criterion.

---

## 1. Repository layout, language, build, test, CI, lint, type rules

### Layout

```
src/nyx/            application package (src layout, pythonpath=["src"])
  adapters/         git_ci/ and agent_transcript/ (Phase 5 / ADR 0010)
  api/              FastAPI app + routes/ + errors + rate_limit
  audit/            hash chain writer, verifier, external checkpoints
  auth/             API-key auth, capability store, principal resolution
  cli/              Typer CLI (`nyx ...`) — one file, main.py (~2000 lines)
  conversational/   chat, MCP server, Telegram bot (Phase 7) — an extra, not core
  critics/          the critic pack c001..c010, base, context, registry, llm
  disputes/         rebuttals, verification jobs, dispositions
  domain/           Pydantic aggregates, enums, ids, canonical, claims, evidence
  evidence/         EMPTY (a placeholder directory with no modules)
  findings/         models, composer, dedup, lifecycle, service
  ingestion/        gateway, redaction, inputs, idempotency, injection, limits, paths
  llm/              provider seam shared by chat and the critic transport
  observability/    logging
  policies/         engine (in-memory versioned rules) and versions (persisted)
  retention/        purge + redaction jobs
  seed/             `nyx seed demo`
  storage/          db.py, tables.py, repositories/
  systems/          monitored-system registry
  watch/            unattended read-only cycle runner
tests/
  unit/             one file per critic (test_critic_c001..c010.py) plus module tests
  integration/      API, CLI contract, disputes, adapters, watch, seed
  contract/         schema/model agreement, adapter registry, LLM parity, API/CLI consistency
  security/         prompt injection, SSRF/paths, evidence access, verification allowlist
  e2e/              EMPTY
  fixtures/         git_ci fixture data only
migrations/versions/ seven Alembic revisions, 9ce2370d7c1a -> f8a2b3c4d5e6
schemas/            eight hand-written JSON Schemas (system, event, claim, evidence, audit,
                    finding, rebuttal, disposition)
examples/           one JSON example per schema, plus examples/ghost-factory/
evals/llm-critic/   corpus.jsonl + thresholds.json (thresholds deliberately `pending`)
scripts/            eval runner, acceptance harness, live probes
tmp/                gitignored scratch (probes written outside the project's test helpers)
```

Two files are **orchestrator-only** by `AGENTS.md` §read-order: `src/nyx/api/app.py` and
`src/nyx/cli/main.py`. Delegated slices deliver modules and tests; the orchestrator wires them.

### Language and stack

- Python 3.14 (venv is 3.14.7; `requires-python = ">=3.12"`, CI pins 3.12 as the declared minimum).
- FastAPI + uvicorn, SQLAlchemy 2.x, Alembic, SQLite file-backed, Pydantic v2, Typer, httpx,
  python-json-logger, `jsonschema` (promoted from dev to runtime for the critic's structured output).
- Optional extra `conversational = ["mcp>=2.3"]`. Everything else is core.
- Hypothesis is a dev dependency and is used (`tests/unit/test_ingestion_properties.py`).

### Commands (from `AGENTS.md` §Commands)

```bash
uv sync
uv run pytest                 # full suite
uv run pytest tests/unit      # unit only
uv run ruff check .           # lint
uv run ruff format .          # format
uv run mypy                   # strict type check over src/
uv run alembic upgrade head   # migrations on the configured database
uv run nyx doctor
uv run nyx --help
```

Pytest config: `testpaths=["tests"]`, `pythonpath=["src"]`,
`addopts="-q --import-mode=importlib"`. Importlib mode exists because two test files share a
basename in different directories.

### CI (`.github/workflows/ci.yml`)

One job, four sequential gates, each failing the job on a non-zero exit: `uv run pytest`,
`uv run mypy`, `uv run ruff check .`, `uv run ruff format --check .`, then a **Migrations** step
against a *fresh* `sqlite:///./ci-fresh.sqlite3`. A second workflow (`security.yml`) runs
`uv audit` on the lockfile and Trivy on the image, weekly plus on push.

### Lint and type rules

- Ruff: `line-length=100`, `target-version="py312"`, `src=["src","tests"]`,
  `select=["E","F","I","UP","B","S","C4","SIM"]`, `ignore=["S101","B008"]` (pytest `assert` and
  FastAPI's `Depends()` default are both deliberate).
- `scripts/*.py` is exempted from `S603`/`S607`/`S110`/`SIM105` because the scripts *are* the
  subprocess callers under test.
- `ruff format` excludes `*.md` (design docs contain illustrative Python that is not source).
- mypy: `strict = true`, `warn_unreachable = true`, `python_version = "3.12"`, `files = ["src"]`.
  **Tests are not type-checked** — mypy's `files` is `src` only.

### Definition of done (AGENTS.md, binding)

Connected end-to-end; happy path, negative path, and the relevant security boundary tested;
docs and sample config updated; `ruff check`, `mypy`, and the relevant tests pass with the real
output reported; no secrets committed; limitations documented; changed files and remaining risks
reported.

---

## 2. The critic interface

### Contract

`src/nyx/critics/base.py` defines `CriticScope`, `CriticResult`, and the `Critic` Protocol:

```python
class Critic(Protocol):
    critic_id: str
    critic_version: str
    description: str
    scope: CriticScope
    critic_type: CriticType
    def run(self, context: CriticContext) -> CriticResult: ...
```

`CriticResult` carries the SPEC §7.2 contract fields (`critic_id`, `critic_version`, `result`,
`proposition`, `scope`, `observations`, `supporting_evidence_ids`, `counterevidence_ids`,
`limitations`, `rationale`, `recommended_next_step`, `requires_human_review`) plus additive
composition hints (`category`, `severity`, `title`, `summary`, `inferences`, `premises`,
`contradiction`, `recommendations`, `unknowns`, `affected_system_ids`, `claim_ids`, `event_ids`,
`confidence`) and the SPEC §13 provenance block (`model_provider`, `model_version`,
`prompt_version`, `resource_usage`, `llm_io`).

**The extension point for this upgrade already exists**: additive fields on `CriticResult` are
explicitly optional, so new fields can be added without breaking a minimal result.

### Input: `CriticContext`

`src/nyx/critics/context.py`. Pure DTO, no `Session`, no writes:

| Field | Source |
| --- | --- |
| `target_event` | the event that triggered the run |
| `linked_claim` | claim the event names (`payload.claim_id`) or that names the event in `source_event_ids` |
| `related_claims` | other stored claims for the same `claimant_system_id` **and same `scope.artifact`**, fetch-limited to 200 (`registry._related_claims`) |
| `linked_evidence` | evidence with `relevance_to_claim == claim.id`, plus any `payload["evidence_ids"]` |
| `scope` | the claim's scope, else the narrowest derivable from the event payload (`artifact`, `commit`, `environment` only) |
| `now` | clock reading taken when the context was built |

### Registration and ordering

`src/nyx/critics/registry.py` holds a module-level `_REGISTRY: dict[str, Critic]`. There is **no
magic discovery**: `register(critic)` is explicit, and `src/nyx/critics/__init__.py` is the single
visible list. C-010 is the exception — it needs a `SessionFactory`, so it is registered by
`register_storage_backed(session_factory)`, which the API factory and the CLI both call with the
factory they already built. Constructing it without a factory returns `ERROR`, never a silent pass.

`run_selected(session, event_id=..., critic_ids=None, clock=utc_now)`:
1. Loads the event, builds the context.
2. Selects critics by id, or **all registered critics in sorted id order** when none are named.
3. For each: `clock()`, run, catch any exception into a `CriticResultStatus.ERROR` result with
   redacted `failure_details`, persist a `CriticRun` row, and — if the result is `FINDING` — call
   `compose_finding` and back-link `run.finding_id`.

Ordering is therefore **sorted by critic id**, and every critic sees the same context. There is no
composition pass and no critic reads another's output. `rerun_for_verification` re-runs the
critics that produced a finding and persists a fresh `CriticRun` linked to the existing finding
without composing a second one; it documents that this is reproduction, not independence.

### Pass / finding representation

`CriticResultStatus` is `FINDING | NO_FINDING | ERROR | SKIPPED | INCONCLUSIVE`. Two results are
not findings and are not composed:

- `INCONCLUSIVE` — used by C-009, which abstains by design on a determinable conflict and sets
  `requires_human_review=True`.
- `ERROR` — used when a critic cannot run at all (C-010 without a factory).

`NO_FINDING` is documented everywhere as "no issue within this critic's declared scope", never as
proof the claim is true. The `no_finding()` helper is duplicated per critic module as a private
function; that is the established house style.

### Versioning

- `critic_version` is a `str` on the critic class and is copied into every `CriticRunRow`.
- The **policy engine** has real versioning (`policies/engine.py`: `Rule(rule_id, version, ...)`,
  `get(id, version=None)` resolves the latest, `digest(id)` hashes the rule's code fingerprint so
  a decision ties to an exact revision).
- `policies/versions.py` persists `PolicyVersionRow` (rule_id, version, digest, changed_by,
  changed_at, change_reason) with a `(rule_id, version)` unique constraint — the acceptance
  scenario 22 requirement that a policy cannot become live without a recorded change.
- **Critic rules themselves have no separate `rule_version` on the wire**; `CriticRunRow.rule_version`
  exists and is nullable but is unused by every current critic. That is a free seam for this
  upgrade (finding-schema extension `critic_version` is already persisted).

### Testing style

One file per critic in `tests/unit/test_critic_c0NN.py`, named in the acceptance table. The pattern
is: construct a `CriticContext` directly with hand-built `Event`/`Claim`/`EvidenceItem` objects
(no database), run the critic, assert on `result.result` and on the specific fields that carry the
finding (`proposition`, `observations`, `supporting_evidence_ids`, `limitations`). Near-miss
cases are the norm (C-006's tests assert that "undeploy" and a blank `approval_ref` stay quiet).
Integration-level coverage lives in `tests/integration/test_findings_readback.py` and
`tests/unit/test_critic_markers.py` (the outcome-marker regexes).

---

## 3. Record, evidence and finding schemas; storage; ingestion

### Domain aggregates (`src/nyx/domain/`)

| Model | File | Notes |
| --- | --- | --- |
| `MonitoredSystem` | `systems.py` | unique `(name, environment)` |
| `Event` | `events.py` | `event_type`, `correlation_id`, `payload`, `actor`, `subject`, `trust_class`, `labels`, `retention_class` |
| `Claim` | `claims.py` | `statement`, `claimant_system_id`, `claim_type`, `scope`, `status`, `retention_class`; **validated to require a scoped claim** (artifact/commit/environment/period/conditions) |
| `EvidenceItem` | `evidence.py` | `kind`, `source_*`, `locator`, `scope`, `captured_at`, `valid_from`/`valid_until`, `content_hash`, `integrity_method`, `producer_identity`, `verification`, `relevance_to_claim`/`relevance_to_finding`/`relevance_note`, `limitations`, `sensitivity`, `access_policy_ref`, `retention_class` |
| `Finding`, `CriticRun`, `Confidence`, `Reasoning`, `RecommendedAction` | `findings/models.py` | see §4 |
| `Rebuttal`, `Disposition` | `disputes/models.py` | append-only, supersede-by-link |
| `AuditEntry` | `audit.py` | hash-chained |
| enums | `enums.py` | 228 lines, all wire values |

**Every model is a `NyxModel`** (`domain/base.py`): frozen, `extra="forbid"`, string-constrained
fields, UTC-only datetimes, prefixed ids (`new_evidence_id()`, …). `extra="forbid"` is why
`test_unknown_fields_agree` exists.

### Storage

`storage/tables.py` maps each aggregate 1:1 to a SQLAlchemy 2.x declarative row with JSON columns
for structured fields and indexes on every filtered column. `EvidenceRow` carries every field the
brief's `Observation` record needs **except** `probe_id`, `collector identity`, `method`,
`subject_version`, `read_only` — those are new. Three rows (`EventRow`, `ClaimRow`, `EvidenceRow`)
carry retention markers (`redacted_at`/`redacted_by`/`redaction_reason`/`redaction_action`) that
are the only columns a tombstone may write.

Repositories live in `storage/repositories/` with a shared `_mapping.py` (`as_utc`). Append-only
tables (`rebuttals`, `dispositions`, `verification_jobs`, `policy_versions`) expose **no** update
or delete.

### Migrations

Seven revisions, single linear chain:

```
9ce2370d7c1a initial -> a13408f58053 evidence scope/findings/critic runs
  -> b2c4d6e8f0a1 idempotency_records -> c4d5e8f0a2b3 disputes/rebuttals/verification/dispositions
  -> e7f9a1b2c3d4 retention redaction markers -> b1c2d3e4f5a6 claims.retention_class
  -> f8a2b3c4d5e6 llm critic run metadata
```

House style: 12-char hex revision id, a module docstring stating the SPEC clause and why the change
is nullable-or-additive, `from collections.abc import Sequence`, `op.add_column(...)` /
`op.create_table(...)` with an explicit `downgrade()`. **Every migration to date is additive** —
no column has been dropped or rewritten, and none should be.

### Ingestion

`ingestion/gateway.py` (686 lines) is the single entry path. Order: size limit → JSON decode →
schema validation → **redaction** → identity dedupe → persist + audit in one transaction, with a
typed per-item outcome. Adapters do **not** bypass it: `adapters/base.py` → `ingestion/inputs.py`
(`EventInput`/`ClaimInput`/`EvidenceInput`) → `IngestionGateway`, so an adapter inherits redaction,
validation, dedupe, and audit for free.

Three producer inputs exist for builds/records/transcripts:

- **Git/CI** (`adapters/git_ci/`, three providers: github, local_git, fixture). Emits
  `EventInput`/`EvidenceInput`. Defect #3 in the Phase 5 notes is the lesson: GitHub says
  `success`/`failure`, which the critics' `pass|green`/`fail|broken|red` markers do **not** match.
- **Agent transcript** (`adapters/agent_transcript/`, ADR 0010). Reads a local SQLite store
  read-only (`mode=ro`), emits `agent.turn` events, one claim per assistant statement ≥12 chars,
  and evidence per tool call + result. Stateless: stable ids derived from the transcript's own ids.
- **Direct API/CLI** (`POST /api/v1/events`, `nyx ingest event`).

---

## 4. Severity, confidence, dedup, reporting

### Severity and confidence

`Severity` is `INFO | LOW | MEDIUM | HIGH | CRITICAL`. It is set by the critic on the
`CriticResult` and copied into the `Finding` by the composer. There is **no computed severity and
no threshold table** — each critic hardcodes its own with a stated rationale. `Confidence` is an
optional `Confidence(value, method)` ("an assessment aid with an explicit method; never a
substitute for evidence"). No current critic sets it.

`FindingCategory` is `CONTRADICTION | UNSUPPORTED_CLAIM | POLICY_VIOLATION | SECURITY_RISK |
QUALITY_RISK | DRIFT | MISSING_VERIFICATION | PROVENANCE_GAP | OTHER`.

### Dedup

`findings/dedup.py`. The key is a **content hash of the identity tuple**
`(critic_id, target_type, target_id, scope.{system_id, artifact, commit, environment, period})` —
never of prose. The docstring states the rule outright: "Merging on text similarity is forbidden:
two incidents can sound alike and be unrelated, and one incident can be described in two ways."
A duplicate is still persisted with `status=duplicate`, a link to the original, and a rationale.
**This is the existing `root_cause_key` seam** — the brief's requirement that one underlying
conflict not raise duplicate findings maps directly onto it, and it currently keys on
(critic, target, scope), not on the *defect*.

### Reporting

`GET /api/v1/findings` filters by system, status, severity, category, date, and cursor;
`GET /api/v1/findings/{id}` returns the finding with evidence metadata. `nyx finding list|show`.
`docs/api.md` is the contract and `docs/openapi.json` is regenerated from the app
(`tests/contract/test_openapi_current.py` fails if it drifts).

### Composer validation (`findings/composer.py`)

The trust boundary. Before persisting it rejects:
1. **Evidence references that do not exist** — looked up in storage, unknown ids named.
2. **Missing category-required fields** — a `contradiction` finding must state `contradiction`;
   an `unsupported_claim` finding must cite at least one evidence item.
3. **A finding that is not inspectable** — at least one observation, and a claim or event reference.
4. **Safety invariant 1** — `confirmed` requires a rationale and ≥1 supporting evidence item.

---

## 5. Config, feature flags, policy, authorisation

### Config (`src/nyx/config.py`, `Settings` / `pydantic-settings`)

`env_prefix="NYX_"`, reads `.env` (env wins), `extra="ignore"`, safe defaults
(`127.0.0.1:8000`, `sqlite:///./data/nyx.sqlite3`). `safe_effective_config` /
`display_effective_config` never render secrets (`_SECRET_MARKERS`, `SecretStr`, and any URL with a
password component are scrubbed by `redact_url`).

The house pattern for a new flag is visible in every phase section: **off by default, fails closed,
named in the message**. Examples already shipped:

- `llm_critic_enabled: bool = False` — "Off by default: the deterministic critic pack is the core".
- `watch_enabled: bool = False` — with `_check_watch_configuration` refusing a watcher with no ref
  or no adapter: "a process that runs, fetches nothing, and reports success".
- `retention_purge_enabled: bool = False` — "Purge is destructive, so it is opt-in".
- `evidence_allow_private_urls: bool = False` — the SSRF default, cross-checked against
  `git_ci_allow_private_targets` so two switches cannot disagree.
- `enabled_critics: list[str]` and `enabled_adapters: list[str]` — the deployment filter.

### Policy (`policies/`)

`engine.py` is an **in-memory** registry of versioned pure-function rules
(`Rule.applies_to` / `Rule.violated` / `Rule.rationale`), with `PolicyRegistry.digest()` tying a
decision to an exact rule revision by hashing the callables' code fingerprints.
`default_registry()` ships three rules: `approval.required-recorded`,
`approval.no-self-approval`, `completion.evidence-required`. `versions.py` persists policy
versions with who changed them.

**There is no declarative policy *data* layer** — rules are Python callables, not typed records.
The brief's c005 requirement ("make policies declarative data with typed rules: forbidden action,
required step, required approver, allowed environment, data-handling limits") is therefore a
**real gap**, not a small extension.

### Authorisation

There is **no `Authorisation` record type**. What exists:

- `auth/` — authentication (API keys, `auth_principals`) and a **capability** store for NYX's own
  API calls (`may(actor, capability)`). This is *NYX's* authorisation, not a record of who approved
  an action taken elsewhere.
- `c006` reads an approval **reference** from the event payload
  (`approval_ref`/`approved_by`/`approval_id`/`authorised_by`/`authorized_by`) or from a linked
  `REVIEW_RECORD`/`POLICY_EVALUATION` evidence item. It checks presence only — never approver ≠
  actor, never scope match, never expiry.
- `policies/engine.py` has `approval.no-self-approval`, which is the only place the
  approver≠actor rule is expressed, and it is a pure function over a payload dict, not a record.

The brief's `Authorisation` evidence kind (who approved what, scope, expiry) is **new**.

---

## 6. Test fixtures and historical real failures usable as golden cases

### Fixture infrastructure

`tests/conftest.py`: a **session-scoped migration template** built by running real
`python -m alembic upgrade head` in a subprocess, copied per test. Tests never use
`Base.metadata.create_all`, so the migration and the `alembic_version` table are exercised by every
integration test. Fixtures: `migration_template`, `database`, `settings`, `engine`,
`session_factory`, `service`, `client`.

### Available golden material

- `examples/ghost-factory/` — six fixtures, one source of truth for the demo seed
  (`nyx seed demo`). `claim-completion-abc123.json`, `evidence-test-abc123.json`,
  `evidence-test-def456.json` (the mismatched-commit pair), `event-completion-claim.json`,
  `system-manifest.json`.
- `tests/fixtures/git_ci/` — adapter fixture data.
- `evals/llm-critic/corpus.jsonl` — 8 labelled synthetic cases covering all three outcome families,
  with a `fixture_response` per case for plumbing-only runs. `thresholds.json` is deliberately
  `pending`: "no numbers are invented here".
- `docs/implementation/audit-findings-2026-10-19.md` — a real audit of NYX by itself.

### Historical real failures — genuinely usable, and better than synthetic cases

The roadmap's per-phase "Defects found while landing the phase" tables are **real, verified
defects with the layer and the shape of each recorded**. These are the highest-value golden cases
because each one is a true positive that the pack missed at the time:

| # | Defect | Phase | Reusable as |
| --- | --- | --- | --- |
| 1 | The audit hash chain had **no test at all**; the plan claimed existing tests guarded it | 4 | c010 / missing-verification |
| 2 | C-010 reached for a private name (`_entry_hash`) instead of the public one | 4 | self-consistency |
| 3 | The capability checker was wired while the actor was still self-asserted, so anyone could claim `operator` | 4 | c006 / authorisation |
| 4 | `nyx finding dispose` could never succeed: the CLI bound deny-all | 4 | config/flag |
| 5 | Rate limiting keyed on a request field nothing set, and middleware runs before auth | 4 | — |
| 6 | **Claims carried no `retention_class`** — the producer's value was validated then silently discarded, so purge skipped every claim forever *while reporting success* | 4 | **c016 claimed-vs-observed side effects** |
| 7 | `nyx doctor` reported any *configured* optional integration as a failure while never checking availability | 4 | **c017 contract conformance** |
| 8 | `configure_logging` replaced `root.handlers`, discarding other components' handlers | 4 | — |
| 9 | `certifi` carried 2 known CVEs | 4 | — |
| 1 | Lifecycle tests cited evidence ids that **no evidence row backed**, so every one of those findings was a self-consistency violation nothing had detected | 5 | **c010** |
| 2 | The adapter's evidence **could not be stored at all** (`EvidenceItem` requires a relevance target) | 5 | c004 |
| 3 | The adapter's evidence was **invisible to C-001** (GitHub says `success`/`failure`, the critic reads `pass|green`/`fail|broken|red`) | 5 | **c001 / c017** |
| 4 | `nyx doctor` reported only what was *configured*, never what was *registered* vs *enabled* | 5 | c017 |

Phase 4 defect #6 and Phase 5 defect #3 are the two the brief's c015 and c017 rules are designed to
catch. Phase 5 defects #2/#3 are the same shape and the roadmap states the lesson: *a slice's own
tests assert the shape of what it returns; they do not assert that the rest of the system can
consume it.*

---

## 7. Where c001..c010 live, and which overlap

| Critic | File | Registered | Scope in one line |
| --- | --- | --- | --- |
| c001 | `critics/c001_completion_vs_verification.py` | `critics/__init__.py` | completion claim vs linked test evidence; missing → `MISSING_VERIFICATION`, failed/stale/mismatched/unreadable → `UNSUPPORTED_CLAIM` |
| c002 | `critics/c002_contradictory_records.py` | `critics/__init__.py` | two **claims** asserting success vs failure for the same artifact/commit/environment with overlapping periods |
| c003 | `critics/c003_evidence_freshness.py` | `critics/__init__.py` | evidence whose declared scope or validity window does not cover the claim |
| c004 | `critics/c004_provenance_gap.py` | `critics/__init__.py` | evidence missing source / producer identity / locator / scope |
| c005 | `critics/c005_policy_violation.py` | `critics/__init__.py` | evaluates the in-memory policy registry against a derived payload |
| c006 | `critics/c006_risky_action_without_authorisation.py` | `critics/__init__.py` | high-impact action token with no approval reference in the payload or a linked review record |
| c007 | `critics/c007_repeated_failure_regression.py` | `critics/__init__.py` | a defined test that passed then failed; reports scope differences as CONFOUNDERS |
| c008 | `critics/c008_unsupported_certainty.py` | `critics/__init__.py` | universal wording **and** exactly one linked verification run |
| c009 | `critics/c009_evidence_conflict.py` | `critics/__init__.py` | two **evidence** items for the same defined test disagreeing under a definitely comparable scope → `INCONCLUSIVE` |
| c010 | `critics/c010_nyx_self_consistency.py` | `register_storage_backed()` | store-global: evidence refs resolve, transitions replay, critic versions recorded, audit chain complete |
| c011 | `critics/llm.py` (`CRITIC_ID = "c011-llm-critic"`) | `register_llm_critic(settings)` | the Phase 6 model critic; **already occupies c011** |

### Overlaps — measured, not assumed

**c002 vs c009 — already separated cleanly, and the boundary is exactly the one the brief asks
for.** Verified in the code:

- c002 compares **`Claim` objects** (`related_claims`, same `claimant_system_id` + same
  `scope.artifact`), reads success/failure markers from `claim.statement`, and requires strict
  same-scope equality (artifact, commit, environment all definitely equal) **plus overlapping time
  periods**. It returns a `FINDING`.
- c009 compares **`EvidenceItem` objects** (`linked_evidence`), reads outcomes from
  `evidence.title`+`description`, groups by `(system, artifact, kind, normalised title)`, requires
  "definitely comparable" scope (commit and environment equal **only when both sides declare
  them**), and returns `INCONCLUSIVE` — never a verdict.

That is precisely "c002 = same-subject records disagreeing over time or between records; c009 =
conflicts between independent evidence sources about the same fact". **The boundary is right; the
gap is that they share no normalisation and no shared key.** Both independently re-derive a
"same defined test" key (c007 and c009 have byte-identical `_test_key`), both re-implement the
outcome regexes (c001, c007, c009 all define `_SUCCESS_MARKER`/`_FAILURE_MARKER` with the same
body), and neither can dedupe against the other. The brief's "if the codebase already separates
them well, only add the shared key" applies: **add a shared claim-graph key, do not restructure
them.**

**Other measured overlaps:**

- **c007 vs c009.** c009 returns `INCONCLUSIVE` on a pass/fail pair at a comparable scope; c007
  returns a `FINDING` on a pass-then-fail sequence. Both can fire on the same evidence with
  different verdicts and nothing reconciles them. Flakiness is the missing discriminator (same
  commit **and** environment → flaky, not a regression) — the research doc's C-016.
- **c001 vs c003.** c001's "stale verification" branch is c003's rule applied to one critic's
  evidence. They are different targets (claim-vs-evidence) so they should both exist, but they
  will double-report the same underlying defect unless `root_cause_key` is shared.
- **c004 vs c010.** Both check evidence integrity; c004 is per-evidence completeness, c010 is
  store-global reference validity. Complementary, not duplicate.
- **c008's own documented blind spot.** c008 returns `no_finding` when a claim has no linked
  evidence, on the stated grounds that "missing verification is the completion critic's finding".
  But c001 returns `no_finding` when the claim is not a `COMPLETION` claim. **A universal claim
  with no evidence and a non-completion type is reported by neither.** This is a live gap that the
  new claim-graph and c012 (skipped required step) / the upgraded c008 can close.
- **`ClaimType` is almost unread.** `grep ClaimType.` over `src/nyx/critics/` returns two hits, both
  `COMPLETION`. Seven of eight claim types are uncriticised.
- **`event_type` is read by no critic.**
- **Evidence metadata is barely read.** `integrity_method`, `verification`, `sensitivity`,
  `access_policy_ref`, `retention_class`, `content_hash` are on `EvidenceItem` and no critic
  inspects any of them. c004 reads `producer_identity`, `locator`, `scope` and the source fields
  only.
- **`related_claims` is fetch-limited to 200 and same-artifact only**, so cross-artifact
  contradiction is structurally out of reach.

---

## 8. Conflicts between this brief and the codebase

Found during discovery. None is blocking; each is resolved by following the codebase's conventions
while keeping the brief's intent, as `AGENTS.md` §Hard-rules 1 and 6 require. Every resolution is
recorded in the final report as a deviation.

| # | Brief says | Codebase says | Resolution |
| --- | --- | --- | --- |
| **C1** | New critics are **c011..c017** | **`c011` is taken**: `CRITIC_ID = "c011-llm-critic"` (Phase 6, SPEC §13), registered by `register_llm_critic()` and documented in `config.py`, `docs/implementation/phase-6-evaluation.md` and `docs/implementation/critic-catalogue-research.md`. | **Renumber the new critics to c012..c018.** Critic ids are public identifiers (`docs/api.md`, `docs/CLI-CONTRACT.md`, the registry's sorted output); renumbering c011 would silently change an existing critic's identity. The brief's *rule set* is unchanged, only the labels. |
| **C2** | c011 = Scope creep | `docs/implementation/critic-catalogue-research.md` already proposes a **different** C-011..C-024 (C-011 claim verifiability, C-012 claim-type coverage, C-015 duplicate evidence, C-016 flaky tests, C-019 external consistency, …). | The research doc is explicitly "research only. Nothing here is implemented". The brief is the authoritative rule set for this task. **Cross-reference the research doc rather than renumber it**, and note in the catalogue that c012..c018 here are a different set from the research doc's c012..c018. |
| **C3** | "Adding a new critic" is a code change | `critic-catalogue-research.md` §6: "adding one is a SPEC change plus an ADR, not a code change alone." SPEC §8 fixes ten critics. | **Update `docs/SPEC.md` §8 and add an ADR** alongside the code. This is additive (the SPEC's own word), so it does not weaken the contract. |
| **C4** | Finding schema extensions listed as new fields | Several already exist: `severity`, `confidence`, `critic_version` (on `CriticRun`, not `Finding`), `dedupe_key`/`duplicate_of`. | Map onto existing fields where they exist; only `root_cause_key`, `observation_refs`, `subject_version`, `reproducibility`, `verification_strength`, `remediation_hint` are genuinely new — and all need a migration. |
| **C5** | c002/c009 "if the codebase already separates them well, only add the shared key" | It does separate them well (verified above). | Add the shared claim-graph key only. **Do not restructure c002 or c009.** |
| **C6** | c006 needs a risk taxonomy with seven named categories | `_HIGH_IMPACT_ACTIONS` is a flat 17-token frozenset matched on whole token or leading segment, deliberately conservative ("an unrecognised verb is NOT treated as high-impact"). | Keep the token set as the *detection* mechanism and add the **taxonomy as a typed, declarative enum** that the tokens map into. Does not weaken the conservative default: an unmapped verb still fires nothing. |
| **C7** | c005 wants "declarative data with typed rules" | Rules are Python callables in an in-memory registry; `digest()` hashes code fingerprints. | Add a declarative `PolicyRule` **data** model alongside the callable registry (the callable stays the evaluation engine). Purely additive. |
| **C8** | "Authorisation" evidence kind with scope and expiry | No authorisation record type exists at all; c006 checks a payload reference's presence only. | New `Authorisation` evidence kind + model + migration. c006 gains the approver≠actor / scope-match / expiry checks **behind a flag** so the existing behaviour and its tests are unchanged when the flag is off. |
| **C9** | Probes are read-only by default, mutating probes behind an authorisation gate | NYX's existing hard rule (ADR 0004, `AGENTS.md` §architecture) is stronger: **"The MVP performs no external action"**, and `c006` states it in every finding's `limitations`. `disputes/verification.py` has the allowlist pattern already. | The QA runner's *drivers* act on a subject by design; NYX itself performs no mutation. Mutating probes use the existing verification-allowlist pattern (`verification.py`) and an authorisation record, gated by a flag that defaults to off. Documented in the safety model. |
| **C10** | QA runner needs a browser driver (Playwright) | Playwright is not a dependency, and `AGENTS.md` §Hard-rules 7 says "prefer boring, well-supported dependencies"; the SPEC §12 dependency review is a gate. | **Do not add Playwright.** Ship the thin vertical slice with the HTTP/API driver (stdlib `httpx`, already a dependency) and the CLI driver; make the browser driver a documented, testable interface with a fixture driver, so it can be added behind a flag after its own dependency review. Deferred, not silently dropped. |
| **C11** | "Add a ground-truth evidence layer" | `src/nyx/evidence/` is an **empty directory** — a placeholder with no modules. `domain/evidence.py` holds the existing model. | Put the new layer in `src/nyx/evidence/` (it is the obvious home and currently unused), keeping `domain/evidence.py` untouched and additive. |
| **C12** | Phase 5 wants a golden set of "at least 20 real, labelled cases from history" | The only labelled corpora are 8 synthetic LLM-eval cases and the roadmap's defect tables (which are real but unlabelled as precision/recall cases). | Build the golden set from **both**: the roadmap's real defects (labelled true-positive by construction — each is a verified defect) plus known-clean controls, reaching ≥20. Document that the labels are derived from recorded post-hoc defects, not from a human labelling session, and state the resulting bias. |

**No blocking contradiction.** The brief's intent is achievable inside the existing architecture.

---

## 9. Implementation plan — phase to file map

Phase numbers below are the brief's. Each row names the real file or module it touches.

### Phase 1 — Ground-truth evidence layer

| Deliverable | Files | Migration |
| --- | --- | --- |
| `VerificationStrength` enum (`none`, `self_reported`, `rerun_by_nyx`, `independently_observed_end_state`) | `src/nyx/domain/enums.py` (additive) | — |
| `EvidenceKind` additions | none needed — `EvidenceKind.RUNTIME_OBSERVATION` and `EvidenceKind.ARTIFACT_DIGEST` already exist | — |
| `Observation` aggregate + `Probe`/`ProbeResult` protocol + the five probe kinds | `src/nyx/evidence/` (new package: `observation.py`, `probes.py`, `registry.py`, `claims.py`) | `observations` table |
| `Authorisation` aggregate (approver, action, scope, expiry) | `src/nyx/evidence/authorisation.py` | `authorisations` table |

The brief's four "kinds of evidence" map onto existing homes as follows, which is why only two
new record types are needed:

| Brief's kind | Existing home | Action |
| --- | --- | --- |
| **Claim** | the `Claim` aggregate (`domain/claims.py`) | none — it is already a first-class, scoped, attributed record |
| **Observation** | nothing (`EvidenceKind.RUNTIME_OBSERVATION` is a *label*, and no critic reads it) | **new `Observation` record + table** |
| **Artifact** | `EvidenceKind.ARTIFACT_DIGEST` + `EvidenceItem.content_hash` | none — the label and the hash field already exist |
| **Authorisation** | nothing (c006 reads a payload *reference* only) | **new `Authorisation` record + table** |
| Claim extraction (deterministic patterns first, LLM-assisted behind an interface) | `src/nyx/evidence/claims.py` | — |
| Probe registry + read-only default + budgets + kill switch | `src/nyx/evidence/registry.py`, `src/nyx/config.py` | — |
| Repositories | `src/nyx/storage/repositories/observations.py`, `authorisations.py` | — |
| `CriticContext.observations` (additive, optional) | `src/nyx/critics/context.py`, `src/nyx/critics/registry.py` (`_build_context`) | — |
| Tests | `tests/unit/test_observation.py`, `test_probes.py`, `test_claim_extraction.py`, `test_evidence_strength.py` | — |

### Phase 2 — Upgrade c001..c010

| Critic | Change | Files |
| --- | --- | --- |
| c001 | require `rerun_by_nyx`+ observation; emit achieved strength; flag off-keep | `critics/c001_*.py`, `tests/unit/test_critic_c001.py` |
| c002/c009 | shared claim-graph normalisation + `root_cause_key`; **no restructure** | new `critics/claim_graph.py`; `c002_*`, `c009_*` |
| c003 | bind freshness to subject version + covered scope | `critics/c003_*` |
| c004 | require producer, method, subject version, artifact hash; optional hash chaining | `critics/c004_*` |
| c005 | declarative typed policy rules beside the callable registry | new `policies/rules.py`; `critics/c005_*` |
| c006 | risk taxonomy + approver≠actor + scope match + expiry (flagged) | `critics/c006_*`, `domain/enums.py` |
| c007 | failure-signature clustering + flaky-vs-regression + recurrence escalation | `critics/c007_*` |
| c008 | certainty vs verification strength + per-agent calibration tracking | `critics/c008_*` |
| c010 | add: missing-evidence citations, duplicate findings, contradictory severities, version drift, judge drift | `critics/c010_*` |
| finding schema extensions | `root_cause_key`, `observation_refs`, `subject_version`, `reproducibility`, `verification_strength`, `remediation_hint` | `findings/models.py`, `critics/base.py`, `findings/composer.py` | `findings` table columns |

### Phase 3 — New critics c012..c018 (renumbered; see conflict C1)

| Critic | Rule | Files |
| --- | --- | --- |
| c012 | Scope creep — changed/accessed outside task or granted permissions | `critics/c012_scope_creep.py` |
| c013 | Skipped required step — absent from a declared process contract (set membership, not order) | `critics/c013_skipped_required_step.py` |
| c014 | Non-determinism — k trials, report pass@k and pass^k | `critics/c014_non_determinism.py` |
| c015 | Swallowed failure — error/warning/non-zero ignored or unrecovered | `critics/c015_swallowed_failure.py` |
| c016 | Claimed vs observed side effects | `critics/c016_claimed_vs_observed_side_effects.py` |
| c017 | Resource drift beyond a threshold against baseline | `critics/c017_resource_drift.py` |
| c018 | Contract conformance — observed behaviour violates the declared contract | `critics/c018_contract_conformance.py` |

Each: `critics/__init__.py` registration, `tests/unit/test_critic_c0NN.py` with ≥1 true positive and
≥1 true negative, `critic_version = "1.0.0"`, `CriticScope` with explicit `cannot_assess`.

### Phase 4 — Final-behaviour QA (black-box)

| Component | Files |
| --- | --- |
| Scenario spec (declarative, versioned) | `src/nyx/qa/spec.py`, `src/nyx/qa/oracles.py` |
| Drivers: HTTP (`httpx`), CLI (subprocess); browser as a **documented interface with a fixture driver** (Playwright deferred — conflict C10) | `src/nyx/qa/drivers/` |
| Per-step perception (screenshot/DOM/console/network/state) | `src/nyx/qa/perception.py` |
| Oracles: deterministic, spec, visual (diff threshold, optional calibrated judge), side-effect | `src/nyx/qa/oracles.py` |
| Reproduction gate (replay from checkpoint, classify deterministic vs flaky) | `src/nyx/qa/reproduce.py` |
| Evidence bundle (content-hashed, redacted) | `src/nyx/qa/bundle.py` |
| Runner + run modes (pre-release, scheduled regression, read-only sampling) | `src/nyx/qa/runner.py`, `src/nyx/config.py` |
| Coverage tracking | `src/nyx/qa/coverage.py` |
| Safety: sandbox/staging, test creds, authorisation gate, budgets, kill switch | `src/nyx/qa/safety.py` |
| Wiring: c016 consumes observations | `critics/c016_*` |
| Tests | `tests/unit/test_qa_*.py`, `tests/integration/test_qa_runner.py` |

**Thin vertical slice first**: one scenario, one driver (HTTP), deterministic + side-effect oracles,
reproduction gate, evidence bundle, c016 wired in. Green before widening.

### Phase 5 — Measuring NYX itself

| Deliverable | Files |
| --- | --- |
| Golden set ≥20 labelled cases (real defects + clean controls) | `evals/golden/golden.jsonl`, `evals/golden/thresholds.json` |
| Precision / recall / FPR per critic; per-judge agreement | `scripts/eval_golden.py` |
| pass@k and pass^k for stochastic components | `scripts/eval_golden.py` |
| Human review queue (low-confidence findings, transcript spot-checks) | `src/nyx/findings/review.py` + CLI/API |
| Saturation tracking → graduate to regression | `src/nyx/qa/saturation.py` |
| CI gate with a documented baseline-update path | `.github/workflows/ci.yml`, `docs/implementation/golden-set.md` |

### Docs (acceptance criterion)

`docs/SPEC.md` §8 (critic catalogue), `docs/evidence-model.md` (four evidence kinds + strength
levels), new `docs/probe-authoring.md`, new `docs/scenario-spec.md`, new `docs/safety-model.md`,
`docs/implementation/roadmap.md` (new phase), `docs/adr/0013-ground-truth-verification.md`,
`docs/api.md` + regenerated `docs/openapi.json`, `.env.example`.

### Commit grouping

One commit per slice, in the repo's style (`feat(critics): ...`, `fix(...): ...`, `docs: ...`,
`chore(tests): ...`), each with the real gate output in the body. No unrelated refactoring.

---

## 10. What this discovery does not establish

- **No thresholds are calibrated.** Every numeric threshold in the brief (resource-drift margin,
  visual-diff threshold, pass-rate cut-off for flaky-vs-deterministic, golden-set pass floor) needs
  measurement against real data. Phase 5 exists to produce those numbers; none is invented here.
- **The golden set's labels are derived, not independently labelled.** The roadmap's defects are
  real and verified, but they were found by the authors' own gates, so the set is biased toward
  defects a gate *could* find. Documented as a limitation, per conflict C12.
- **No claim is made that the QA runner can detect real bugs before it is run.** The acceptance
  criterion is that it detects a *deliberately broken* sample app and does not flag a
  flaky-by-design scenario. That is a plumbing proof, not a capability claim.
- **Playwright is deliberately absent** (conflict C10). The browser driver is an interface, not an
  implementation.
- **`src/nyx/evidence/` is empty and its intended contents are unrecorded.** Putting the new layer
  there is a judgement, not a documented decision.
