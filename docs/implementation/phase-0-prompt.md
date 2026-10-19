# Task: NYX Phase 0 — Repository discovery, architecture and decisions

You are implementing NYX in this repository (`D:\NYX`). Read these first, in order:

1. `docs/SPEC.md` — the full contract. Part I is research rationale, Part II is binding.
2. `AGENTS.md` — binding operating rules for you. Follow them exactly.

## Current state (already done, do not redo)

The scaffold exists and is verified working:

- `pyproject.toml` — uv project, Python 3.14.7 venv at `.venv`, deps installed
  (fastapi, uvicorn, pydantic v2, pydantic-settings, sqlalchemy 2.1, alembic, typer, httpx,
  python-json-logger; dev: pytest, hypothesis, ruff, mypy).
- Empty module directories exist under `src/nyx/`: api, cli, domain, ingestion, evidence, critics,
  findings, disputes, policies, audit, storage, adapters, observability.
- `tests/{unit,integration,contract,security,e2e,fixtures}`, `schemas/`, `examples/`, `migrations/`,
  `docs/adr/`, `docs/implementation/` exist.
- `uv run pytest` passes (1 smoke test), `uv run ruff check .` passes, `uv run mypy` passes.
- Git is initialised. Nothing is committed yet beyond the scaffold.

## Your task for this run: Phase 0 only

Deliver Phase 0 exactly as SPEC.md §19 "Phase 0" and §2 require. Produce these files:

1. `docs/implementation/current-state.md`
   - Reconnaissance report: what exists now, what is reusable, what is missing. Facts only, no
     speculation about code that does not exist yet.

2. `docs/architecture.md`
   - Module map for every directory under `src/nyx/`, each with: responsibility, what it must NOT do,
     and its public interface sketch.
   - The separation-of-concerns table from SPEC.md §4 ("Separation of concerns"), adapted to the real
     module names.
   - Explicit statement that Phase 1 ships these as one deployable service, not microservices.

3. `docs/threat-model.md`
   - Trust boundaries from SPEC.md §3.1.
   - The ten safety invariants from SPEC.md §3.2, restated as testable requirements.
   - The full threat/defense table from SPEC.md §8.
   - Explicit non-claims: what NYX cannot establish (e.g. absence of uninstrumented events).

4. `docs/evidence-model.md`
   - The evidence categories from SPEC.md §6 and the hierarchy from §7.1.
   - A clear statement that a hash/signature is an integrity identifier, not proof of truth.
   - What each `integrity_method` value does and does not establish.

5. `docs/implementation/roadmap.md`
   - Phases 0-6 from SPEC.md §19 with, for each: deliverable, exit criteria, status
     (`not started` for all except Phase 0 = `in progress`), and the acceptance scenarios from §17.2
     that gate it.

6. `docs/implementation/acceptance-tests.md`
   - All 24 acceptance scenarios from SPEC.md §17.2 as a checklist, each mapped to the phase that
     must satisfy it and the test file that should own it. All unchecked.

7. `docs/implementation/task-checklist.md`
   - The 17-step task order from SPEC.md §20 as a checkbox list, with the file(s) each step is
     expected to create or change.

8. `docs/adr/` — write these ADRs, one file each, using the format:
   `# NNNN. Title` / `Status:` / `Context` / `Decision` / `Consequences` / `Alternatives considered`.
   - `0001-language-and-framework.md` — Python 3.14 + FastAPI + Pydantic v2, chosen because it
     matches the sibling `Drift` project, gives the strongest property-based testing story via
     Hypothesis, and keeps the service single-process and local-first.
   - `0002-storage-sqlite-sqlalchemy-alembic.md` — SQLite via SQLAlchemy 2.x with Alembic
     migrations; no second database, no message broker in the MVP.
   - `0003-append-only-history-over-mutation.md` — findings, rebuttals and dispositions are
     append-only; every disposition supersedes but never deletes its predecessor.
   - `0004-no-external-action-in-mvp.md` — NYX recommends, never acts; enforcement belongs to a
     separate authorised mechanism.
   - `0005-evidence-is-not-proof.md` — why NYX keeps evidence references and limitations rather than
     collapsing everything into a single confidence score.
   - `0006-untrusted-content-boundary.md` — all ingested content is untrusted data; instructions
     inside it never change policy or grant tool access.

9. `docs/api-error-conventions.md`
   - The structured error contract: machine-readable `code`, human `message`, `request_id`, optional
     `field_details`. Give concrete example payloads and the initial error-code catalogue.

10. `docs/privacy-and-retention.md`
    - Data classification (`public`/`internal`/`confidential`/`restricted`), redaction-before-
      persistence rule, retention classes, and the deletion/redaction workflow from SPEC.md §11.3.
    - State plainly that a hash can itself be sensitive.

11. `schemas/` — JSON Schema (draft 2020-12) files:
    `event.schema.json`, `claim.schema.json`, `evidence.schema.json`, `finding.schema.json`,
    `rebuttal.schema.json`, `disposition.schema.json`, `system.schema.json`, `audit.schema.json`.
    Base every required field on SPEC.md §5. Include `schema_version` in each. Make them valid JSON
    and genuinely restrictive (types, enums, required, `additionalProperties: false` where sensible).
    These must be hand-written JSON Schema for now; note in each file's `description` that Phase 1
    must add a test proving the Pydantic models and these schemas agree.

12. `examples/` — `system-manifest.json`, `event.json`, `claim.json`, `evidence.json`,
    `rebuttal.json`. Each must validate against its schema. Use the synthetic Ghost Factory demo from
    SPEC.md §21 as the running example.

13. `docs/implementation/phase-0-prompt.md` — a copy of the task instructions you were given.

## Rules for this run

- Write real content, not stubs. No `TODO: fill in` placeholders.
- Do NOT write implementation code yet. No `src/nyx/**` modules in this run. Phase 0 is documents,
  schemas, and examples only.
- Validate every JSON file you write actually parses. You may run:
  `uv run python -c "import json,glob; [json.load(open(f)) for f in glob.glob('schemas/*.json')+glob.glob('examples/*.json')]; print('all json ok')"`
- Do not invent facts about the sibling projects (ATHENA, ARGUS, DANNY, NOESIS, DMRX, Ghost
  Factory). Refer to them only as the spec does — intended integration targets whose interfaces have
  not yet been inspected.
- Do not claim anything is verified that you did not run.

## When finished, report

- The list of files created.
- The exact command(s) you ran to validate the JSON, and their real output.
- Anything in the spec you deliberately deferred to a later phase, and why.
