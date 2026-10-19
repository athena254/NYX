# NYX implementation roadmap

**Source:** `docs/SPEC.md` §19. Phase numbers and exit criteria are taken from that section; this file
tracks status, deliverables, and the acceptance scenarios that gate each phase.
**Last updated:** 19 October 2026.

Status values: `not started`, `in progress`, `blocked`, `done`.

## Summary

| Phase | Name | Status | Acceptance scenarios that gate it |
| --- | --- | --- | --- |
| 0 | Repository discovery and decisions | done | None numbered; documents, schemas, examples, and ADRs reviewed |
| 1 | Evidence and event spine | in progress (1a done; 1b not started) | 1, 2, 3, 14, 18 |
| 2 | Deterministic critic engine | not started | 4, 5, 6, 7, 8, 9, 12, 24 |
| 3 | Disputes and verification | not started | 10, 11, 22, 23 |
| 4 | Security and operational hardening | not started | 13, 14, 15, 16, 17, 18, 19, 20 |
| 5 | Integrations | not started | No numbered scenarios; adapter contract tests and independent disablement |
| 6 | Optional model critics and calibration | not started | 19 (re-validated with model critics) plus evaluation-corpus thresholds |

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

**Status:** in progress. Phase 1a (domain model, storage spine, system registry) is complete; Phase
1b (HTTP API, CLI ingestion, redaction pipeline, idempotency) is not started.

**Deliverable:** a working local service that can register a system, ingest and retrieve a normalized
event, claim, and evidence reference, persist them through migrations, and trace each record to its
source.

**Work items**

- System registry, event/claim/evidence Pydantic models, database tables, and Alembic migration.
- Repositories and transaction boundaries.
- Ingestion API and CLI, including batch ingestion with per-item outcomes.
- Canonicalisation, content hashes, redaction before persistence, payload and batch limits,
  idempotency, and deduplication with recorded rationale.
- Append-only audit entries for accepted mutations.
- Local fixtures and a working `nyx doctor`.
- Schema/model agreement test for all `schemas/*.schema.json` files.
- Seed the Ghost Factory demonstration data from SPEC §21.

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

**Explicit non-goals for this phase:** critics beyond a stub registry, findings, disputes, and any
external adapter.

---

## Phase 2 — Deterministic critic engine

**Status:** not started.

**Deliverable:** a deterministic critic engine that turns the Phase 1 records into evidence-linked
findings without an LLM.

**Work items**

- Critic interface, registry, versioning, and critic-run persistence.
- C-001 through C-007.
- C-008 through C-010.
- Finding composition with observation/inference/recommendation/unknown separation.
- Finding deduplication, severity, querying, and the lifecycle state machine.
- C-001 end-to-end: completion claim, mismatched commit, evidence-linked finding.
- Negative case: same-commit passing evidence prevents a false positive.

**Exit criteria (SPEC §19):** deterministic critics are testable without an LLM and acceptance
scenarios 4–9, 12, and 24 pass.

**Acceptance scenarios:** 4, 5, 6, 7, 8, 9, 12, 24.

**Explicit non-goals for this phase:** rebuttals, dispositions, verification jobs, LLM critics.

---

## Phase 3 — Disputes and verification

**Status:** not started.

**Deliverable:** an append-only dispute workflow with verification jobs and authorised dispositions.

**Work items**

- Rebuttal submission and counterevidence links.
- Verification requests and queue/runner using allowlisted checks.
- Disposition submission with capability checks and required rationale.
- Supersession links; no deletion of prior findings, rebuttals, or dispositions.
- History API/CLI.
- Transactional state transitions with audit entries.
- Policy versioning for disposition rules.

**Exit criteria (SPEC §19):** acceptance scenarios 10–11 and 22–23 pass.

**Acceptance scenarios:** 10, 11, 22, 23.

**Explicit non-goals for this phase:** performing remediation, external enforcement, or closing a
finding merely because the target says it is fixed.

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
