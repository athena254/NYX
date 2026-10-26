# NYX implementation roadmap

**Source:** `docs/SPEC.md` §19. Phase numbers and exit criteria are taken from that section; this file
tracks status, deliverables, and the acceptance scenarios that gate each phase.
**Last updated:** 24 October 2026.

Status values: `not started`, `in progress`, `blocked`, `done`.

## Summary

| Phase | Name | Status | Acceptance scenarios that gate it |
| --- | --- | --- | --- |
| 0 | Repository discovery and decisions | done | None numbered; documents, schemas, examples, and ADRs reviewed |
| 1 | Evidence and event spine | done (1a and 1b) | 1, 2, 3, 14, 18 |
| 2 | Deterministic critic engine | done (2a and 2b) | 4, 5, 6, 7, 8, 9, 12, 24 |
| 3 | Disputes and verification | done | 10, 11, 22, 23 |
| 4 | Security and operational hardening | done | 13, 14, 15, 16, 17, 18, 19, 20, 21 |
| 5 | Integrations | done | No numbered scenarios; adapter contract tests and independent disablement |
| 6 | Optional model critics and calibration | done (26 October 2026) | 19 (model path), 15 (model path), evaluation corpus; live calibration recorded and thresholds `current` |
| 7 | Conversational interface | done | No numbered scenarios; the chat cannot create a finding or record a disposition |

Scenarios 14 and 18 are first gated in Phase 1 and re-validated in Phase 4. Scenario 19 is first
gated in Phase 4 for safe failure handling and re-validated in Phase 6 with an actual LLM adapter.

---

## Phase 0 — Repository discovery and decisions

**Status:** done.

**Deliverable:** a reviewed implementation plan with mapped files and tests.

**Work items**

- [x] Inspect the repository and report current state: `docs/implementation/current-state.md`.
- [x] Confirm language, framework, and database from existing conventions.
- [x] Create the architecture and threat-model documents.
- [x] Define event, claim, evidence, finding, rebuttal, disposition, system, and audit schemas.
- [x] Define the structured API error contract.
- [x] Define data classification, redaction, and retention policy.
- [x] Record key decisions in ADRs 0001–0006.
- [x] Map acceptance scenarios and the 17-step task order to expected test and source files.
- [x] Review the Phase 0 documents and schemas with the project owner.
- [x] Complete the dependency/license/maturity review in SPEC §12 before adding any integration
      dependency. **Done 21 October 2026** — `docs/implementation/phase-5-plan.md` §1 is the audit:
      every installed package with its version and licence read from the venv, the candidates
      considered and rejected with reasons, and the verdict that Phase 5 adds **no** new third-party
      dependency (`httpx`, already installed, plus the standard library).

**Exit criteria (SPEC §19):** the team can explain which facts NYX can observe, which it cannot, and
what each evidence source actually establishes. The supporting explanation is in
`docs/evidence-model.md` and `docs/threat-model.md`.

**Acceptance scenarios:** no numbered section 17.2 scenario gates Phase 0. The gate is review of the
documents, schemas, and examples.

---

## Phase 1 — Evidence and event spine

**Status:** done (19 October 2026). Phase 1a (domain model, storage spine, system registry) and
Phase 1b (HTTP API, CLI ingestion, redaction pipeline, idempotency, readiness) are complete.

**Deliverable:** a working local service that can register a system, ingest and retrieve a normalized
event, claim, and evidence reference, persist them through migrations, and trace each record to its
source.

**Work items**

- [x] System registry, event/claim/evidence Pydantic models, database tables, and Alembic migration.
- [x] Repositories and transaction boundaries.
- [x] Ingestion API and CLI, including batch ingestion with per-item outcomes.
- [x] Canonicalisation, content hashes, redaction before persistence, payload and batch limits,
      idempotency, and deduplication with recorded rationale.
- [x] Append-only audit entries for accepted mutations.
- [x] Local fixtures, retrieval endpoints, and a working `nyx doctor`.
- [~] Schema/model agreement test for the five record types that have Pydantic models (system,
      event, claim, evidence, audit). `finding`, `rebuttal`, and `disposition` schemas have no model
      until Phase 2/3 and are explicitly deferred.
- [ ] Seed the Ghost Factory demonstration data into a local database. The source fixtures exist in
      `tests/fixtures/`; a seed command is deferred until findings can consume it (Phase 2).

**Exit criteria (SPEC §19):** acceptance scenarios 1–3, 14, and 18 pass.

**Acceptance scenarios:** 1, 2, 3, 14, 18.

### Phase 1a — Domain model, storage spine, system registry (complete)

**Status:** done (19 October 2026).

Delivered and verified by `uv run pytest`:

- [x] Pydantic v2 domain models for `MonitoredSystem`, `Event`, `Claim`, `EvidenceItem`, and
      `AuditEntry`, with shared enums, UTC-only timestamps, stable prefixed IDs, and canonical JSON.
- [x] Secret-ish `metadata` keys rejected at the domain boundary; unscoped claims rejected.
- [x] SQLAlchemy 2.x tables, indexes, repositories, engine/session factory, and `session_scope`.
- [x] Alembic environment and a real initial migration; `uv run alembic upgrade head` succeeds.
- [x] Settings with loopback/SQLite safe defaults, secret-safe effective configuration, `.env.example`.
- [x] Append-only audit writer with a documented local hash chain.
- [x] Idempotent system registry with audit entries.
- [x] Tests: canonicalisation (including a Hypothesis key-shuffle property), domain validation,
      system registry, migrations, and configuration redaction.
- [x] Minimal `nyx doctor`; `uv run nyx doctor` passes against a migrated local database.

Not delivered in 1a: HTTP API, CLI ingestion commands, redaction of incoming payloads, event/claim/
evidence ingestion workflow, idempotency keys, schema/model agreement test, and fixtures. These are
Phase 1b or later work.

### Phase 1b — HTTP API, CLI, redaction, idempotency (complete)

**Status:** done (19 October 2026). Verified by `uv run pytest` (unit, integration, contract, and
Hypothesis property tests), `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
and `uv run alembic upgrade head`.

Delivered:

- [x] Ordered ingestion pipeline: size limit → JSON decode → schema validation → redaction →
      identity dedupe → persistence + audit in one transaction, with a typed result per record.
- [x] Producer input models with tolerated server fields and optional `producer_event_id`.
- [x] Redaction rules for private keys, JWTs, bearer tokens, URL credentials, AWS access keys,
      API-key-like strings, secret assignments/keys, and high-entropy values. Reports rule names and
      JSON paths only; fails closed with `REDACTION_FAILED`.
- [x] Identity-based deduplication with a persisted `idempotency_records` table, migration, and
      recorded rationale. Never merges on content similarity.
- [x] Per-item transactional batch ingestion; one rejected item does not roll back accepted items.
- [x] HTTP subset under `/api/v1`: systems (register/list), events (ingest/retrieve/batch),
      claims (register/retrieve), evidence (register/retrieve), health, readiness.
- [x] Structured error envelope, request IDs, cursor pagination, and `DATABASE_NOT_READY`.
- [x] CLI: `system register|list`, `ingest event|batch`, `claim add`, `evidence add`, and an
      expanded `doctor`; `--json` everywhere and non-zero exits on failure.
- [x] Fixtures for the Ghost Factory demonstration; schema/model agreement test for the five
      records with Pydantic models.
- [x] `docs/api.md`, `docs/operations.md`, and the regenerated `docs/openapi.json`.

Known limitations:

- No authentication/authorisation or rate limits (Phase 4); the API is for local/trusted use.
- Dedupe scope is per record type, not per source system; `producer_event_id` must be unique within
  a type.
- `GET /api/v1/findings*` is absent by design (Phase 2).
- Findings, rebuttals, dispositions, verification, and adapters remain out of scope.

**Explicit non-goals for this phase:** critics beyond a stub registry, findings, disputes, and any
external adapter.

---

## Phase 2 — Deterministic critic engine

**Status:** done (20 October 2026). Phase 2a (critic engine, finding composer, C-001..C-004) and Phase 2b
(C-005..C-010, the policy engine, and the finding lifecycle state machine) are complete.

**Deliverable:** a deterministic critic engine that turns the Phase 1 records into evidence-linked
findings without an LLM.

**Work items**

- [x] Critic interface, registry, versioning, and critic-run persistence.
- [x] C-001 through C-004.
- [x] C-005 through C-010.
- [x] Finding composition with observation/inference/recommendation/unknown separation.
- [x] Finding deduplication, severity, querying, and the lifecycle state machine.
- [x] C-001 end-to-end: completion claim, mismatched commit, evidence-linked finding.
- [x] Negative case: same-commit passing evidence prevents a false positive.

**Exit criteria (SPEC §19):** deterministic critics are testable without an LLM and acceptance
scenarios 4–9, 12, and 24 pass.

**Acceptance scenarios:** 4, 5, 6, 7, 8, 9, 12, 24.

### Phase 2a — Critic engine, composer, C-001..C-004 (complete)

**Status:** done (19 October 2026).

Delivered:

- [x] Critic interface (`critics/base.py`), registry (`critics/registry.py`), and `CriticRun`
      persistence with type/version/inputs/timestamps/outcome.
- [x] `critics/__init__.py` registers C-001..C-004 at import.
- [x] Finding composer separating observations, inferences, recommendations, and unknowns.
- [x] Finding deduplication, severity, querying, and the read-only findings API/CLI surface.
- [x] C-001 completion claim vs. verification (scenarios 4–6).
- [x] C-002 contradictory records (scenario 7), C-003 evidence freshness (scenario 8),
      C-004 provenance gap.
- [x] Unit tests for scenarios 6–9 and a C-001 fix for false negatives in the success/failure markers.

### Phase 2b — Full critic pack, policy engine, lifecycle machine (complete)

**Status:** done (20 October 2026). Verified with `uv run pytest` (185 tests passing), `uv run mypy`
(81 files, strict), `uv run ruff check .` (121 files), and `uv run ruff format --check .`.

Delivered:

- [x] Finding lifecycle state machine (`findings/lifecycle.py`) implementing the SPEC §6 graph as
      data, with per-target and per-edge safety rules, a capability seam, and an audit entry for
      every accepted transition (scenario 12).
- [x] C-005 policy violation with a built-in versioned rule engine (`policies/engine.py`) that
      resolves rules, reports the exact policy ID/version, carries the input record, and raises on
      an unknown policy ID. Each violation names the policy ID and version, carries the input
      record payload, and sets `category=policy_violation`.
- [x] C-006 risky action without authorisation. Seven controls against three positive tests; an
      ordinary action, a blank `approval_ref`, and the substring "undeploy" all stay quiet. Its
      findings state in their own limitations that NYX performed nothing (safety invariant 6).
- [x] C-007 repeated failure regression, C-008 unsupported certainty, C-009 evidence conflict,
      C-010 NYX self-consistency.
- [x] C-009 returns `INCONCLUSIVE`, not a finding, on a conflicting-evidence case: a critic that
      guesses on ambiguous evidence is worse than one that declines.
- [x] C-010 registered through `register_storage_backed()` so the API factory and the CLI both pass
      the session factory they already built; constructed without one it returns an `ERROR` result
      rather than a silent pass.

Known limitations:

- Scenario 24 ("every confirmed finding passes the NYX self-consistency critic") is **met** as of
  21 October 2026. C-010 is enforced as a gate on `confirmed` and `partially_confirmed` by
  `nyx.findings.lifecycle`; see the "Scenario 24 note" in `acceptance-tests.md`.
- The capability seam is now wired to real authentication (`src/nyx/auth/`, Phase 4); the
  `DenyAllCapabilityChecker` default remains for callers that construct the lifecycle directly.
- `docs/implementation/task-checklist.md` step 12 (audit chain verification) is closed: `nyx audit
  verify` and `tests/integration/test_audit_integrity.py` landed in Phase 4.

**Explicit non-goals for this phase:** rebuttals, dispositions, verification jobs, LLM critics.

---

## Phase 3 — Disputes and verification

**Status:** done (20 October 2026). Verified with `uv run pytest` (243 tests passing), `uv run mypy`
(strict, 91 files), `uv run ruff check .` (139 files), and `uv run alembic upgrade head`.

**Deliverable:** an append-only dispute workflow with verification jobs and authorised dispositions.

**Work items**

- [x] Rebuttal submission and counterevidence links.
- [x] Verification requests and queue/runner using allowlisted checks.
- [x] Disposition submission with capability checks and required rationale.
- [x] Supersession links; no deletion of prior findings, rebuttals, or dispositions.
- [x] History API/CLI.
- [x] Transactional state transitions with audit entries.
- [x] Policy versioning for disposition rules.

**Exit criteria (SPEC §19):** acceptance scenarios 10–11 and 22–23 pass.

**Acceptance scenarios:** 10, 11, 22, 23.

### What was built

- **Models and storage** (`src/nyx/disputes/models.py`, four append-only tables, one migration):
  `Rebuttal`, `VerificationJob`, `Disposition`, `PolicyVersion`. A disposition supersedes by storing
  the previous disposition's id; the previous row is never rewritten.
- **Policy versioning** (`src/nyx/policies/versions.py`): `PolicyVersionStore.register` makes a rule
  live and records who changed it in one call, so a policy cannot become active without a recorded
  change (scenario 22).
- **Verification allowlist** (`src/nyx/disputes/verification.py`): a check that is not registered
  cannot run, and an unregistered id is refused before anything executes. The built-in check re-runs
  the critics that produced a finding against current records, records a new `CriticRun`, composes no
  second finding, and states that it is a reproduction check rather than independent corroboration.
- **Dispute service** (`src/nyx/disputes/service.py`): rebuttals, verification jobs, dispositions, and
  history. Every state change goes through `FindingLifecycle`.
- **API and CLI**: nine dispute endpoints and `nyx finding rebut|verify|dispose|history`.
- **`docs/dispute-protocol.md`**: the challenge loop end to end with a worked example.

### Two rules worth recording

1. **A challenged finding must name what was weighed.** A rebuttal is an answerable challenge, so a
   challenged finding cannot be dismissed as `rejected` or `inconclusive` without recording the
   counterevidence considered. The rule is scoped to findings that actually have a rebuttal, so an
   unchallenged finding can still be rejected on its own terms.
2. **A verdict is reachable only from `verification_pending`**, so `record_disposition` walks the
   canonical path there first, the same way `submit_rebuttal` walks to `rebuttal_pending`. A finding
   already past that point has no edge back, and the verdict is validated against its actual state.

**Explicit non-goals for this phase:** performing remediation, external enforcement, or closing a
finding merely because the target says it is fixed.

### Known limitations

- **No authentication.** The actor is taken from the request body, so every dispute record is
  self-asserted until Phase 4 replaces it with a verified principal. The audit trail states this.
- **The capability checker defaults to deny-all**, so a default deployment accepts rebuttals and
  refuses dispositions. That is the safe direction and is deliberate.
- **Scenario 16 is deliberately not checked.** "A verification tool cannot execute an arbitrary
  command supplied by an untrusted event" is Phase 4-owned; checking a Phase 4 box from Phase 3
  would break the ownership rule. Phase 3's obligation is narrower: do not create the hole. The
  allowlist is unit-tested to refuse unregistered checks.
- **Audit-chain verification (`nyx audit verify`) is Phase 4.** The staged prompt asked for it here;
  the plan recorded why it does not belong in this phase.

---

## Phase 4 — Security and operational hardening

**Status:** done (21 October 2026). Verified with `uv run pytest` (545 passed, 1 skipped), `uv run
mypy` (strict, 120 files), `uv run ruff check .`, `uv run ruff format --check .` (194 files), and
`uv run alembic upgrade head` on a clean database. Acceptance scenarios 13–21 pass; the evidence is
in `acceptance-tests.md` and each slice's commit message.

**Deliverable:** enforceable security boundaries and operational readiness for local and controlled
deployment.

**Work items**

- [x] Complete threat-model implementation and security tests. `tests/security/` is new: 5 files.
- [x] Authentication and capability-based authorisation. `src/nyx/auth/`; the actor on a lifecycle
      transition is the authenticated principal, so a caller cannot record an action under another
      identity. ADR 0007 records the trust decision.
- [x] Rate limits, bounded ingestion, and safe error/logging behaviour. Sliding-window per-caller
      limiting, keyed by principal when authenticated and by socket peer otherwise; `/health` and
      `/ready` exempt.
- [x] Retention and redaction jobs. `src/nyx/retention/`; `nyx retention purge` and
      `nyx retention redact`. Purge is opt-in and tombstones rather than deletes.
- [x] Prompt-injection, SSRF, path-traversal, and arbitrary-command tests. All three guards have a
      real caller at the ingestion edge, so none is a placeholder module.
- [x] Health and readiness checks; `nyx doctor` secret-safe output. `--json` now carries an
      `effective_config` key rendered by the redacting config renderer.
- [x] Audit verification within its documented model. `nyx audit verify`; the hash chain now has one
      shared implementation used by the writer, the verifier, and C-010.
- [x] Dependency and container scanning in CI. `.github/workflows/`; a `Dockerfile` and `.dockerignore`
      so there is an image to scan.
- [x] API/CLI consistency tests. `tests/contract/test_api_cli_consistency.py`.

**Exit criteria (SPEC §19):** acceptance scenarios 13–20 pass. **Met, and 21 as well** — the
roadmap's work item "API/CLI consistency tests" had no scenario assigned to it; scenario 21 is the
one that owns it.

**Acceptance scenarios:** 13, 14, 15, 16, 17, 18, 19, 20, 21.

**Explicit non-goals for this phase:** model critics and external adapters.

### Defects found while landing the phase

None of these were reported by the agents that wrote the slices; each was found by running the
project's own gates or a probe written outside the project's test helpers.

| # | Defect | Layer |
| --- | --- | --- |
| 1 | The audit hash chain had **no test at all** — the plan claimed existing tests guarded it, and that claim was false | audit |
| 2 | C-010 reached for a private name (`_entry_hash`) instead of the public one | critics |
| 3 | The capability checker was wired while the actor was still self-asserted, so anyone could claim the `operator` identity and be granted its capability | auth |
| 4 | `nyx finding dispose` could never succeed: the CLI bound deny-all too | CLI |
| 5 | Rate limiting keyed on a request field nothing set, and middleware runs before auth | API |
| 6 | **Claims carried no `retention_class`** — the value producers sent was validated then silently discarded, so purge skipped every claim forever while reporting success | domain/storage |
| 7 | `nyx doctor` reported any *configured* optional integration as a failure while never checking availability | CLI |
| 8 | `configure_logging` replaced `root.handlers`, discarding handlers other components installed; a closed stream then raised on emit and printed a `--- Logging error ---` block | observability |
| 9 | `certifi 2026.10.18` carried 2 known CVEs, found by running the dependency scan locally | dependencies |

---

## Phase 5 — Integrations

**Status:** done (22 October 2026). Six slices (5.0–5.5), one adapter (Git/CI) with three
providers (GitHub, local git, fixture). Verified with `uv run pytest` (646 passed, 1 skipped),
`uv run mypy` (129 files), `uv run ruff check .`, `uv run ruff format --check .` (211 files),
`uv run alembic upgrade head`, the boundary grep (G7) and the security allowlist (G1), and
`tmp/probes/probe_git_ci_adapter.py` (34 checks against a real socket and a real migrated
database).

**Deliverable:** optional adapters that can be enabled independently while core NYX still works.

**Work items**

- [x] Complete the SPEC §12 dependency/license/maturity review before adding any integration
      dependency. `docs/implementation/phase-5-plan.md` §1 is the audit: **no new third-party
      dependency** — `httpx` (already installed, BSD-3-Clause) plus the standard library.
- [x] Shared adapter infrastructure: `Adapter` protocol, registry, health, config fields
      (slice 5.0), ADR 0008, and the boundary grep (gate G7).
- [x] Git/CI adapter: repository, commit, run, artifact, and check associations
      (slices 5.1–5.4).
- [x] Offline fixture provider (slice 5.5).
- [x] Contract test, permissions, failure modes, and disablement test for the adapter
      (slices 5.3 and 5.5).
- [x] Documented disablement path: `Settings.enabled_adapters` is the deployment filter;
      `AdapterRegistry.registered()` is the code inventory; `nyx doctor` reports both.

**Exit criteria (SPEC §19):** adapters can be disabled independently and core NYX still works.
**Met.** `tests/integration/test_adapter_disablement.py` asserts all eight disablement
properties, including the one that matters most: with the adapter enabled and its host
unreachable, `health()` is `DEGRADED` and `GET /api/v1/ready` is still `200`.

**Acceptance scenarios:** no numbered section 17.2 scenario gates Phase 5; the adapter has a
contract test (slice 5.3) and a documented disablement path (slice 5.5).

**Explicit non-goals for this phase:** assuming any sibling project already exposes a compatible
interface, or granting an adapter more authority than its declared capability.

### Slices

| Slice | Commit | What |
| --- | --- | --- |
| 5.0 | `c0bc402` | `Adapter` protocol, registry, health, config fields, ADR 0008, boundary grep |
| 5.1 | `f652fcb` | `GitHubCiClient`, `GitCiConfig` — bounded GET, secret-safe, private-target guard |
| 5.2 | `f652fcb` | `normalize.py` — GitHub JSON to `EventInput` / `EvidenceInput` |
| 5.3 | `524afed` | `GitCiAdapter` + the contract test (25 cases over a real socket) |
| 5.4 | `524afed` | `LocalGitClient` — same interface; `.git` read with `pathlib`; packed/loose objects added by ADR 0011 (no subprocess) |
| 5.5 | `b3d5240` | `FixtureCiClient` + the disablement test (the phase's exit criterion) |

### Defects found while landing the phase

None of these were reported by the agents that wrote the slices; each was found by running the
project's own gates or a probe written outside the project's test helpers.

| # | Defect | Layer |
| --- | --- | --- |
| 1 | The lifecycle tests cited evidence ids that **no evidence row backed**, so every one of those findings was a self-consistency violation nothing had detected. Found while closing scenario 24. | tests |
| 2 | The adapter's evidence **could not be stored at all**: `EvidenceItem` requires a relevance target, and an observing adapter produces evidence before any claim references it. SPEC §5.4 vs §14. | domain |
| 3 | The adapter's evidence was **invisible to C-001**: the critic reads `pass\|green` / `fail\|broken\|red` from the evidence text, and GitHub says `success`/`failure`, which match neither. | adapter/critic seam |
| 4 | `nyx doctor` reported only what was *configured*, never what was *registered* vs *enabled*, so an installed-but-disabled adapter was invisible to its operator. | CLI |

**#2 and #3 are the same shape and the important lesson.** A slice's own tests assert the
*shape* of what it returns; they do not assert that the rest of the system can *consume* it.
Both defects lived exactly at that seam — one at the domain boundary, one at the critic
boundary — and both were caught by driving the output through the real ingestion path.

### Honest limitations, recorded rather than hidden

- **The local-git provider now reads loose and packed commit/tree objects** (ADR 0011), so a run
  carries the real message, author, parents, and changed paths. **Textual diffs are still not
  produced**, and a shallow/pruned store degrades to the reflog for that commit.
- **`EvidenceItem` now accepts unlinked evidence with a `relevance_note`.** This is an explicit
  SPEC §5.4 deviation, recorded in ADR 0008, and reversible.
- **OpenTelemetry and the evaluation/policy/provenance adapters are not in Phase 5.** They are
  separate adapters needing their own dependency review; the Git/CI adapter is the phase.
- **The adapter is not wired into `nyx doctor`'s reachability probe.** Doctor reports what is
  registered and enabled; it does not claim an availability it never checked.
- **No sibling adapter exists.** §7 of the plan is reconnaissance only; it licenses nothing.

---

## Phase 6 — Optional model critics and calibration

**Status:** done (26 October 2026). Slices 6.1–6.5 delivered the provider seam, opt-in wiring and
provenance persistence, model-path injection/failure tests, the fixture evaluation corpus and
runner, and docs. **The live calibration run was performed 26 October 2026** against the
owner-approved provider and `evals/llm-critic/thresholds.json` is `current` rather than
`pending`. The plan that closed the phase is `docs/implementation/phase-6-finish-plan.md`; the
recorded runs and the defects they exposed are in `docs/implementation/phase-6-evaluation.md`.

**Deliverable:** a pluggable LLM critic behind the same interface as deterministic critics, with
documented calibration and failure behaviour.

**Work items**

- [x] Provider seam: `src/nyx/llm/provider.py`, shared by the async chat client and the sync
      critic transport; `src/nyx/conversational/llm.py` remains a re-export shim.
- [x] `SyncCriticTransport` over `httpx.Client`, with one deadline shared by critic and provider
      and a parity contract test against the async client over a real socket.
- [x] Opt-in registration: c011 is registered only when `NYX_LLM_CRITIC_ENABLED=true`, and
      enabling it without `NYX_LLM_MODEL` refuses at startup. Wired in the API factory and CLI.
- [x] Strict structured-output validation, evidence-ID validation, refusal handling, and
      model-confidence rejection, exercised over the real transport.
- [x] Provenance persistence: provider, model, prompt-template version, token usage, and a
      nullable `llm_io` written only when `NYX_LLM_LOG_PROMPTS=true` and redacted before storage.
- [x] Prompt-injection tests on the model path (`tests/security/test_llm_critic_injection.py`).
- [x] Provider-failure tests over a local socket
      (`tests/integration/test_llm_critic_provider_failures.py`).
- [x] Evaluation corpus, runner, and recorded fixture-mode results
      (`evals/llm-critic/`, `scripts/eval_llm_critic.py`,
      `docs/implementation/phase-6-evaluation.md`).
- [x] **Live calibration run, recorded; thresholds replaced with measured floors.** Two runs were
      recorded (`docs/implementation/evidence/live-run-2026-10-26*.json`). The model leg scored
      0.125 accuracy against the deterministic baseline's 0.75, and the reason is recorded rather
      than hidden: the model abstains on six of eight cases.
- [x] Caveat hardening (owner-approved plan, `docs/implementation/caveats-plan.md`): Apache-2.0
      licence (H1); local-git loose **and** packed object reading with the standard library (H2a,
      H2b, ADR 0011); external audit checkpoints (H3, ADR 0012); opt-in `nyx doctor --probe` (H4);
      `nyx seed demo` and the missing ADR 0010 (H5).

**Exit criteria (SPEC §19):** schema validation, evidence-ID validation, prompt-injection tests,
provider-failure tests, and documented evaluation results pass. **Met.** All five are proven by
tests; the evaluation is documented with two recorded live runs and a `current` thresholds file.

**Acceptance scenarios:** 19 (model path), 15 (model path), plus the evaluation-corpus thresholds
defined in this phase. The corpus has eight synthetic cases across all three outcome families;
fixture-mode scores are a plumbing check, not a model-quality claim.

**Explicit non-goals for this phase:** treating model output as deterministic verification, using
model confidence as calibrated probability without separate calibration, or automatic enforcement.

### Defects found while landing the phase

None of these were reported by the tests that wrote the slices; each was found by driving the
real provider.

| # | Defect | Layer |
| --- | --- | --- |
| 1 | A markdown-fenced answer was rejected as "not valid JSON", so a case the model had answered was recorded as a transport failure | model path |
| 2 | The drafting prompt referenced a JSON Schema it never showed, so the model invented a shape | golden set |
| 3 | `nyx golden generate` reported a failure count with no reason | CLI |
| 4 | A 250-character requirement was dropped by a 200-character field | golden set |
| 5 | Redaction destroyed a schema-constrained identifier, and the failure looked like a producer error | ingestion |

### Honest limitations, recorded rather than hidden

- **The model leg abstains.** On six of eight corpus cases it returns `inconclusive`. That is the
  critic's documented safe direction, not a defect, but it means the model path contributes less
  than the deterministic pack on this corpus.
- **The model leg is not deterministic.** One case failed as invalid JSON in one run and returned
  valid JSON on a direct re-probe. Two runs of eight cases are a sample, not a measurement.
- **`min_accuracy: 0.125` is a collapse floor, not a quality claim.** Eight cases means one case
  is 0.125 of the score.
- **The golden set is eight cases, awaiting review.** Eight cases from `docs/SPEC.md`, all
  seedable, all reaching the expected verdict (accuracy 1.0). The guardrail that capped the set
  at two was the *length of the instruction block*; shortening it — with the schema, the
  injection defence and the untrusted-data fence all kept — took the yield from 2 to 8. What is
  left is provider instability and the 20-case target. See
  `docs/implementation/golden-set-run.md`.

---

## Phase 7 — Conversational interface

**Status:** done (21 October 2026; the missing ADR 0010 was written 24 October 2026). See
`docs/implementation/conversational-interface-plan.md`.
Slices 6.1, 6.2, 6.3, 6.4 (CLI chat and Telegram bot), and 6.6 are done.

**Deliverable:** a conversational front-end for NYX, living in the NYX repository as a separate
independent unit, with the CLI as the machine interface for other systems. The default LLM backend
is the dmr-X inference gateway, consumed over HTTP.

**Work items**

- [x] Harden the CLI for other systems: `--json` and exit codes on every command, contract-tested.
      `tests/integration/test_cli_contract.py` (12 tests) asserts the surface; `docs/CLI-CONTRACT.md`
      documents it. Three defects found and fixed, one in the deterministic core.
- [x] NYX MCP server exposing NYX's operations as tools (read and write), with no tool that can
      create a finding or record a disposition. 12 tools, live-verified over stdio.
- [x] LLM provider adapter: two protocols (OpenAI-compatible `/v1/chat/completions` and Anthropic
      `/v1/messages`) behind one interface, so any Anthropic or OpenAI-compatible LLM works.
- [x] dmr-X as the default inference backend, called over HTTP — dmr-X holds the API keys, so NYX
      needs none. Its code is not vendored; both projects stay independent.
- [x] Conversational front-end: `nyx chat` (interactive CLI) and the **Telegram bot**
      (`nyx telegram serve`). The bot drives the same `ChatAgent`, so it inherits the safety
      property; it adds fail-closed admission (private chats, an explicit allowlist) and a
      per-user rate limit. `tests/unit/test_conversational_telegram.py` (65 tests) and
      `scripts/probe_telegram_live.py` prove it.
- [x] Provider-failure, timeout, and prompt-injection handling.

**Exit criteria:** a user can ask about stored findings and get an answer citing real finding IDs;
can file a rebuttal and request verification by chatting; and **no code path exists** by which the
conversational layer can create a finding or record a disposition. The same three properties hold
for the Telegram bot, which is a second entry point onto the same tool surface rather than a second
implementation of it.

**Acceptance scenarios:** no numbered scenario gates this phase. It is gated by the "the LLM cannot
judge" property, proven by test.

**Explicit non-goals for this phase:** LLM-generated findings or dispositions; NYX as a
general-purpose assistant that performs actions.

### The rule this phase exists to protect

> **The language model is the interface, not the judge.**

NYX's value is that its verdicts are deterministic and evidence-linked. The conversational layer has
exactly the powers a human has through the API and no more: it can query anything, file a rebuttal,
and request verification. It cannot create a finding or change a lifecycle state.
