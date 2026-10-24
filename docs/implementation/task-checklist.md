# Coding-agent task order — SPEC §20

This is the 17-step implementation sequence. Steps 1–3 were completed in Phase 0 as documentation and
schema artifacts; the runtime code they describe is still Phase 1 work. All other steps are unchecked.

Legend: `[x]` = the named artifact exists and was inspected; `[~]` = partially implemented in
the Phase 1a run with the gap named; `[ ]` = not started. A checked step here is not a claim that
its runtime behaviour is implemented or verified.

| Step | Task | Expected files created or changed | Status |
| --- | --- | --- | --- |
| 1 | Repository reconnaissance and current-state report. | `docs/implementation/current-state.md` | [x] |
| 2 | Architecture, threat model, and ADRs. | `docs/architecture.md`, `docs/threat-model.md`, `docs/evidence-model.md`, `docs/adr/0001`–`0006` | [x] |
| 3 | Schemas and domain invariants. | `schemas/*.schema.json`, `examples/*.json`, `src/nyx/domain/*`, `tests/contract/test_schema_agreement.py` | [x] Domain models, enums, IDs, canonical JSON, timestamp and scope validators exist; the agreement test covers system, event, claim, evidence, and audit. `finding`, `rebuttal`, and `disposition` have no Pydantic model until Phase 2/3 and are explicitly deferred. |
| 4 | Storage, migrations, and repositories. | `src/nyx/storage/*`, `migrations/versions/*`, `alembic.ini`, `tests/integration/test_storage.py`, `tests/integration/test_migrations.py` | [~] Phase 1a: tables, indexes, repositories, session boundary, initial migration, and `test_migrations.py` exist. `test_storage.py` is not written. |
| 5 | Event ingestion and system registry. | `src/nyx/ingestion/*`, `src/nyx/api/routes/systems.py`, `src/nyx/api/routes/events.py`, `src/nyx/cli/main.py`, `tests/integration/test_api_contract.py`, `tests/integration/test_ingestion_idempotency.py` | [x] Phase 1b: registry, ingestion pipeline, retrieval, batch API, CLI commands, and `doctor` are implemented and tested. |
| 6 | Evidence registry, redaction, hashing, and access controls. | `src/nyx/domain/evidence.py`, `src/nyx/storage/repositories/evidence.py`, `src/nyx/ingestion/redaction.py`, `tests/unit/test_redaction.py`, `tests/integration/test_redaction_persistence.py`, `tests/security/test_evidence_access.py` | [~] Phase 1b: evidence ingestion, canonical hashes, and redaction before persistence are done and tested. Access-control enforcement is Phase 4. |
| 7 | Critic interface and critic registry. | `src/nyx/critics/base.py`, `src/nyx/critics/registry.py`, `src/nyx/critics/scheduler.py`, `tests/unit/test_critic_registry.py` | [x] Phase 2a: interface, registry, versioning, `CriticRun` persistence, and critic-run API/CLI exist and are tested. `scheduler.py` is not implemented; critics are invoked on demand via the run-critics endpoint. |
| 8 | C-001 completion-vs-verification critic. | `src/nyx/critics/c001_completion_vs_verification.py`, `tests/unit/test_critic_c001.py` | [x] Phase 2a: implemented and tested (scenarios 4–6), including the same-commit negative case. |
| 9 | Finding composition, query, and lifecycle state machine. | `src/nyx/findings/*`, `src/nyx/domain/lifecycle.py`, `tests/unit/test_finding_lifecycle.py`, `tests/integration/test_findings_api.py` | [x] Phase 2a/2b: composer, dedup, read-only findings surface, and the SPEC §6 lifecycle machine exist and are tested (scenario 12, including a Hypothesis property over transition sequences). The state machine lives in `src/nyx/findings/lifecycle.py`, not `domain/lifecycle.py`; `test_findings_api.py` is not written. |
| 10 | Remaining deterministic critics (C-002–C-010). | `src/nyx/critics/c002_*.py` … `c010_*.py`, `tests/unit/test_critic_c002.py` … `test_critic_c010.py` | [x] Phase 2a/2b: C-002..C-010 all exist, are registered, and have unit tests. The built-in versioned policy engine lives in `src/nyx/policies/engine.py`. |
| 11 | Rebuttal, verification, and disposition workflows. | `src/nyx/disputes/*`, `src/nyx/api/routes/disputes.py`, `tests/integration/test_disputes.py` | [x] Phase 3: rebuttals, allowlisted verification jobs, dispositions with supersession links, and history, through the service, API, and CLI. Routes live in `src/nyx/api/routes/disputes.py` rather than `findings.py`, which stays read-only. |
| 12 | Audit verification and self-consistency checks. | `src/nyx/audit/*`, `src/nyx/critics/c010_nyx_self_consistency.py`, `tests/integration/test_audit_integrity.py` | [x] Phase 1a/2b/4: append-only audit writer with a documented hash chain; C-010 registered and tested in both directions; chain verification (`nyx audit verify`, `test_audit_integrity.py`) landed in Phase 4. C-010 is now also **enforced as a gate on `confirmed`** (acceptance scenario 24). |
| 13 | CLI and API contract completion. | `src/nyx/cli/*`, `src/nyx/api/*`, `docs/api.md`, `tests/contract/test_api_cli_consistency.py`, generated OpenAPI checked in | [x] Phase 1b/4/7: the full API surface, `docs/api.md`, and a drift-tested `docs/openapi.json` exist. Phase 4 added `tests/contract/test_api_cli_consistency.py`; Phase 7 added `tests/integration/test_cli_contract.py` (12 tests) and `docs/CLI-CONTRACT.md`. |
| 14 | Security hardening and negative tests. | `src/nyx/api/auth.py`, `src/nyx/api/errors.py`, `tests/security/*`, `.github/workflows/*`, `docs/threat-model.md` updates | [x] Phase 4: authentication and capability enforcement (`src/nyx/auth/`), rate limiting, retention/redaction, prompt-injection/SSRF/path-traversal guards, `tests/security/` (5 files), CI workflows, and a Dockerfile. |
| 15 | Git/CI adapter. | `src/nyx/adapters/git_ci/*`, `tests/contract/test_git_ci_adapter.py`, `docs/adr/0008-adapter-boundary-and-disablement.md` | [x] Phase 5 (slices 5.0–5.5, commits `c0bc402`–`b3d5240`): the `Adapter` protocol and registry, `GitHubCiClient`, `normalize.py`, `GitCiAdapter` + contract test, `LocalGitClient`, `FixtureCiClient` + the disablement test. ADR number corrected — 0007 is `0007-auth-modes.md` (Phase 4). |
| 16 | Optional model critics and other adapters. | `src/nyx/critics/llm/*`, `src/nyx/adapters/*`, `tests/unit/test_llm_critic_failure.py`, evaluation corpus fixtures | [x] Phase 4 delivered the failure-safe stub; Phase 5 delivered the Git/CI adapter. **Phase 6 (24 Oct 2026):** c011 is wired through `nyx.llm` + `SyncCriticTransport`, opt-in registration, provenance and privacy-gated I/O persistence, model-path injection/provider-failure suites, and a fixture evaluation corpus/runner. The **live calibration run and numeric thresholds are pending owner approval**; tracing/evaluation/policy/provenance adapters are later phases. |
| 17 | End-to-end demo and operational documentation. | `examples/*`, `tests/e2e/test_ghost_factory_demo.py`, `docs/operations.md`, `docs/implementation/roadmap.md` status updates | [~] Phase 1b: Ghost Factory fixtures, `docs/operations.md`, `docs/api.md`, and roadmap updates exist. The end-to-end critic demonstration is Phase 2. |

## Phase 1a run note (19 October 2026)

Phase 1a implemented and tested the domain model, storage spine, configuration, audit writer, and
system registry. It deliberately did not implement the HTTP API, ingestion commands, critics,
findings, disputes, or adapters. No numbered acceptance scenario is marked complete by this run;
see `acceptance-tests.md`.

## Phase 1b run note (19 October 2026)

Phase 1b completed steps 5, 6 (redaction/hashing portion), 13 (Phase 1 subset), and 17 (fixtures and
operational documentation). It added the ingestion pipeline, HTTP routes and retrieval, CLI ingestion,
readiness, identity-based idempotency, test fixtures, and the schema/model agreement test. Access
controls (step 6/14), API/CLI consistency (step 13), the end-to-end critic demo (step 17), and the
seed command remain open. Verified with `uv run pytest` (89 tests), `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy`, and `uv run alembic upgrade head`.

## Phase 2 run note (20 October 2026)

Phase 3 completed step 11. Phase 2a and 2b completed steps 7–10 (with the gaps named inline above). They added the critic
interface and registry, `CriticRun` persistence, the finding composer, finding dedup and the read-only
findings surface, all ten deterministic critics (C-001..C-010), the built-in versioned policy engine,
and the SPEC §6 finding lifecycle state machine. Step 11 (rebuttals, verification, dispositions) is
the whole of Phase 3. Verified with `uv run pytest` (185 tests), `uv run mypy` (strict, 81 files),
`uv run ruff check .` (121 files), and `uv run ruff format --check .`.

## First demonstrable vertical slice (SPEC §20)

The first slice must be end-to-end:

> ingest a scoped completion claim → attach a test result tied to a commit → run a deterministic
> critic → create an evidence-linked finding when the commit differs → submit a rebuttal → verify →
> record the disposition and full history.

Do not begin with an LLM critic or a dashboard. The Ghost Factory demonstration in SPEC §21 is the
reference scenario for steps 8–11 and 17.

## Caveat-hardening run note (24 October 2026)

The owner-approved plan `docs/implementation/caveats-plan.md` closed every caveat it named. This is
what landed, each verified against the code rather than the plan:

- **H1 — licence.** `LICENSE` (Apache-2.0), `pyproject.toml` `license` + classifier, README
  §License, and a dependency-licensing section in `docs/operations.md`.
- **H2a/H2b — local-git objects.** `src/nyx/adapters/git_ci/objects.py` reads loose and packed
  commit/tree objects, including OFS and REF deltas, with `zlib` only — no new dependency and no
  subprocess under `src/`. `LocalGitClient` enriches each run from real objects and keeps
  `conclusion=None`. ADR 0011. **Textual diffs are still not produced**; a path-change set against
  the first parent is the maximum claim, and a shallow or pruned store degrades to the reflog.
- **H3 — external audit checkpoints.** `nyx audit checkpoint` appends `{sequence, head_hash,
  recorded_at, entries_checked}` to a JSONL file; `nyx audit verify --against FILE` compares the
  stored chain against it. A fully recomputed rewrite no longer verifies. ADR 0012. A missing,
  empty, short, ahead-of-chain, or malformed checkpoint is an explicit refusal, never a pass.
- **H4 — `nyx doctor --probe`.** Opt-in. One bounded `GET` against the configured provider's
  `/v1/models` and `health()` for every **enabled** adapter. A probe failure is a failed check,
  because the operator asked. Without the flag, no external system is contacted.
- **H5 — seed demo and the missing ADR.** `nyx seed demo` loads `examples/ghost-factory/` through
  the ordinary ingestion gateway and is idempotent by identity. The fixtures are now the single
  source of truth: the tests read the same directory, so a fixture cannot drift from what the seed
  ingests. ADR 0010, referenced by the code and by commit `cdab828` but absent from `docs/adr/`, is
  written.

### Defects found while landing these, and fixed

- `display_effective_config` echoed any URL setting verbatim, so a password embedded in
  `NYX_LLM_BASE_URL` was printed by `nyx doctor`. Redaction now covers every URL, not just the
  database URL.
- `build_adapter_registry` constructed every adapter unconditionally, so `nyx doctor` failed on any
  deployment with no Git/CI repository configured — the most common configuration there is. A
  disabled-but-misconfigured adapter is now described rather than raised.
- `httpx` logs every request at INFO, which put a log line on stdout in front of the JSON a caller
  was parsing. The CLI now suppresses the HTTP client's and alembic's own loggers for the duration
  of a command.
- The seed exposed a replay mislabelled as `created`: a manifest whose id already matches the stored
  row is exactly the replay case, and the gateway reported it as a write.

Verified with `uv run pytest` (819 passed, 1 skipped), `uv run mypy` (146 files), `uv run ruff check .`,
`uv run ruff format --check .` (249 files), and `uv run alembic upgrade head`.
