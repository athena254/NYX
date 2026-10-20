# NYX implementation roadmap

**Source:** `docs/SPEC.md` §19. Phase numbers and exit criteria are taken from that section; this file
tracks status, deliverables, and the acceptance scenarios that gate each phase.
**Last updated:** 19 October 2026.

Status values: `not started`, `in progress`, `blocked`, `done`.

## Summary

| Phase | Name | Status | Acceptance scenarios that gate it |
| --- | --- | --- | --- |
| 0 | Repository discovery and decisions | done | None numbered; documents, schemas, examples, and ADRs reviewed |
| 1 | Evidence and event spine | done (1a and 1b) | 1, 2, 3, 14, 18 |
| 2 | Deterministic critic engine | done (2a and 2b) | 4, 5, 6, 7, 8, 9, 12; 24 partial |
| 3 | Disputes and verification | done | 10, 11, 22, 23 |
| 4 | Security and operational hardening | not started | 13, 14, 15, 16, 17, 18, 19, 20 |
| 5 | Integrations | not started | No numbered scenarios; adapter contract tests and independent disablement |
| 6 | Optional model critics and calibration | not started | 19 (re-validated with model critics) plus evaluation-corpus thresholds |
| 7 | Conversational interface | planned | No numbered scenarios; the chat cannot create a finding or record a disposition |

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
- [ ] Complete the dependency/license/maturity review in SPEC §12 before adding any integration
      dependency. Installed versions and licenses are recorded in `current-state.md`; no independent
      security or maturity audit has been performed.

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

- Scenario 24 ("every confirmed finding passes the NYX self-consistency critic") is **partially**
  met. The C-010 critic exists, is registered, and is tested in both directions (a consistent store
  stays quiet; a store with a missing lifecycle transition or a tampered audit entry fires). It is
  **not** enforced as a gate on `confirmed`, so a finding can currently be confirmed without C-010
  being consulted. The box in `acceptance-tests.md` therefore stays unchecked.
- The capability seam is still `DenyAllCapabilityChecker` until Phase 4 wires real authentication.
- `docs/implementation/task-checklist.md` step 12 (audit chain verification) remains open.

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

**Status:** not started.

**Deliverable:** enforceable security boundaries and operational readiness for local and controlled
deployment.

**Work items**

- Complete threat-model implementation and security tests.
- Authentication and capability-based authorisation.
- Rate limits, bounded ingestion, and safe error/logging behaviour.
- Retention and redaction jobs.
- Prompt-injection, SSRF, path-traversal, and arbitrary-command tests.
- Health and readiness checks; `nyx doctor` secret-safe output.
- Audit verification within its documented model.
- Dependency and container scanning in CI.
- API/CLI consistency tests.

**Exit criteria (SPEC §19):** acceptance scenarios 13–20 pass.

**Acceptance scenarios:** 13, 14, 15, 16, 17, 18, 19, 20.

**Explicit non-goals for this phase:** model critics and external adapters.

---

## Phase 5 — Integrations

**Status:** not started.

**Deliverable:** optional adapters that can be enabled independently while core NYX still works.

**Work items**

- Git/CI adapter first: repository, commit, run, artifact, and check associations.
- OpenTelemetry/OpenInference ingestion where practical.
- Optional evaluation, policy, and provenance adapters after their contracts are inspected.
- Ecosystem-specific adapters for ATHENA, ARGUS, DANNY, NOESIS, DMRX, and Ghost Factory only after
  their current APIs, licenses, maintenance, and data-access constraints are inspected.
- Contract test, permissions, failure modes, and disablement test for every adapter.

**Exit criteria (SPEC §19):** adapters can be disabled independently and core NYX still works.

**Acceptance scenarios:** no numbered section 17.2 scenario gates Phase 5; each adapter must have a
contract test and a documented disablement path.

**Explicit non-goals for this phase:** assuming any sibling project already exposes a compatible
interface, or granting an adapter more authority than its declared capability.

---

## Phase 6 — Optional model critics and calibration

**Status:** not started.

**Deliverable:** a pluggable LLM critic behind the same interface as deterministic critics, with
documented calibration and failure behaviour.

**Work items**

- LLM critic adapter with externally configured provider/model, timeout, and no-LLM mode.
- Strict structured-output validation and evidence-ID validation.
- Prompt-injection tests against model critics.
- Provider-failure, timeout, refusal, and invalid-output handling.
- Evaluation corpus from synthetic and consented examples.
- Comparison with the deterministic baseline: false positives, false negatives, unsupported
  assertions, latency, and cost.
- Independent review and versioned scoring rules.

**Exit criteria (SPEC §19):** schema validation, evidence-ID validation, prompt-injection tests,
provider-failure tests, and documented evaluation results pass.

**Acceptance scenarios:** 19 (re-validated with model critics), plus the evaluation-corpus thresholds
defined in this phase. Scenario 15 is re-validated against model critics.

**Explicit non-goals for this phase:** treating model output as deterministic verification, using
model confidence as calibrated probability without separate calibration, or automatic enforcement.

---

## Phase 7 — Conversational interface

**Status:** planned (20 October 2026). See `docs/implementation/conversational-interface-plan.md`.

**Deliverable:** a standalone conversational front-end for NYX, with the CLI as the machine
interface for other systems.

**Work items**

- [ ] Harden the CLI for other systems: `--json` and exit codes on every command, contract-tested.
- [ ] NYX MCP server exposing NYX's operations as tools (read and write), with no tool that can
      create a finding or record a disposition.
- [ ] LLM provider adapter: OpenAI-compatible `base_url` + `api_key` + `model`, ported from the
      proven pattern in The-Agency's `agency/llm/`.
- [ ] Conversational front-end: an interactive CLI chat first, then a Telegram bot.
- [ ] Provider-failure, timeout, and prompt-injection handling.

**Exit criteria:** a user can ask about stored findings and get an answer citing real finding IDs;
can file a rebuttal and request verification by chatting; and **no code path exists** by which the
conversational layer can create a finding or record a disposition.

**Acceptance scenarios:** no numbered scenario gates this phase. It is gated by the "the LLM cannot
judge" property, proven by test.

**Explicit non-goals for this phase:** LLM-generated findings or dispositions; NYX as a
general-purpose assistant that performs actions.

### The rule this phase exists to protect

> **The language model is the interface, not the judge.**

NYX's value is that its verdicts are deterministic and evidence-linked. The conversational layer has
exactly the powers a human has through the API and no more: it can query anything, file a rebuttal,
and request verification. It cannot create a finding or change a lifecycle state.
