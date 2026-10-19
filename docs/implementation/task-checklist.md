# Coding-agent task order — SPEC §20

This is the 17-step implementation sequence. Steps 1–3 were completed in Phase 0 as documentation and
schema artifacts; the runtime code they describe is still Phase 1 work. All other steps are unchecked.

Legend: `[x]` = artifact exists in this repository and was inspected; `[ ]` = not started. A checked
step here is not a claim that its runtime behaviour is implemented or verified.

| Step | Task | Expected files created or changed | Status |
| --- | --- | --- | --- |
| 1 | Repository reconnaissance and current-state report. | `docs/implementation/current-state.md` | [x] |
| 2 | Architecture, threat model, and ADRs. | `docs/architecture.md`, `docs/threat-model.md`, `docs/evidence-model.md`, `docs/adr/0001`–`0006` | [x] |
| 3 | Schemas and domain invariants. | `schemas/*.schema.json`, `examples/*.json`, `src/nyx/domain/*`, `tests/contract/test_schema_agreement.py` | [x] (Phase 0 schemas/docs only; runtime models and agreement test are Phase 1) |
| 4 | Storage, migrations, and repositories. | `src/nyx/storage/*`, `migrations/versions/*`, `alembic.ini`, `tests/integration/test_storage.py`, `tests/integration/test_migrations.py` | [ ] |
| 5 | Event ingestion and system registry. | `src/nyx/ingestion/*`, `src/nyx/api/routes/systems.py`, `src/nyx/api/routes/events.py`, `src/nyx/cli/main.py`, `tests/integration/test_ingestion_api.py`, `tests/integration/test_idempotency.py` | [ ] |
| 6 | Evidence registry, redaction, hashing, and access controls. | `src/nyx/evidence/*`, `src/nyx/domain/redaction.py`, `tests/security/test_redaction.py`, `tests/security/test_evidence_access.py` | [ ] |
| 7 | Critic interface and critic registry. | `src/nyx/critics/base.py`, `src/nyx/critics/registry.py`, `src/nyx/critics/scheduler.py`, `tests/unit/test_critic_registry.py` | [ ] |
| 8 | C-001 completion-vs-verification critic. | `src/nyx/critics/c001_completion_vs_verification.py`, `tests/unit/test_critic_c001.py` | [ ] |
| 9 | Finding composition, query, and lifecycle state machine. | `src/nyx/findings/*`, `src/nyx/domain/lifecycle.py`, `tests/unit/test_finding_lifecycle.py`, `tests/integration/test_findings_api.py` | [ ] |
| 10 | Remaining deterministic critics (C-002–C-010). | `src/nyx/critics/c002_*.py` … `c010_*.py`, `tests/unit/test_critic_c002.py` … `test_critic_c010.py` | [ ] |
| 11 | Rebuttal, verification, and disposition workflows. | `src/nyx/disputes/*`, `src/nyx/api/routes/findings.py`, `tests/integration/test_disputes.py` | [ ] |
| 12 | Audit verification and self-consistency checks. | `src/nyx/audit/*`, `src/nyx/critics/c010_self_consistency.py`, `tests/integration/test_audit_integrity.py` | [ ] |
| 13 | CLI and API contract completion. | `src/nyx/cli/*`, `src/nyx/api/*`, `docs/api.md`, `tests/contract/test_api_cli_consistency.py`, generated OpenAPI checked in | [ ] |
| 14 | Security hardening and negative tests. | `src/nyx/api/auth.py`, `src/nyx/api/errors.py`, `tests/security/*`, `.github/workflows/*`, `docs/threat-model.md` updates | [ ] |
| 15 | Git/CI adapter. | `src/nyx/adapters/git_ci/*`, `tests/contract/test_git_ci_adapter.py`, `docs/adr/0007-git-ci-adapter.md` | [ ] |
| 16 | Optional model critics and other adapters. | `src/nyx/critics/llm/*`, `src/nyx/adapters/*`, `tests/unit/test_llm_critic_failure.py`, evaluation corpus fixtures | [ ] |
| 17 | End-to-end demo and operational documentation. | `examples/*`, `tests/e2e/test_ghost_factory_demo.py`, `docs/operations.md`, `docs/implementation/roadmap.md` status updates | [ ] |

## First demonstrable vertical slice (SPEC §20)

The first slice must be end-to-end:

> ingest a scoped completion claim → attach a test result tied to a commit → run a deterministic
> critic → create an evidence-linked finding when the commit differs → submit a rebuttal → verify →
> record the disposition and full history.

Do not begin with an LLM critic or a dashboard. The Ghost Factory demonstration in SPEC §21 is the
reference scenario for steps 8–11 and 17.
