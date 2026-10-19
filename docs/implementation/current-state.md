# Current state — Phase 0 reconnaissance

**Inspected:** 19 October 2026, working tree at commit `9f1cd21` ("Scaffold NYX: uv/Python project,
module layout, spec, agent rules").
**Scope:** repository contents only. No external project was inspected.

## 1. What exists

### 1.1 Repository and tooling

| Item | Observed state |
| --- | --- |
| Root | `D:\NYX` |
| Git | Initialised; one commit (`9f1cd21`); no tags observed |
| Python project | `pyproject.toml` using Hatchling, package `nyx`, source layout `src/nyx` |
| Python requirement | `requires-python = ">=3.12"` |
| Virtual environment | `.venv`, Python `3.14.7` |
| Lockfile | `uv.lock` present |
| Test runner | pytest, `testpaths = ["tests"]`, `pythonpath = ["src"]` |
| Lint | Ruff, line length 100, target `py312` |
| Types | mypy strict, `mypy_path = "src"`, `files = ["src"]` |
| CLI entry point | `nyx = "nyx.cli.main:app"` declared in `[project.scripts]` |
| CI | `.github/workflows/` exists but is empty |
| Agent rules | `AGENTS.md` present and binding |
| Contract | `docs/SPEC.md` present |

### 1.2 Installed dependencies

Versions read from the active `.venv` with `importlib.metadata`. License values were read from the
public PyPI JSON metadata for the same version on 19 October 2026. No independent license,
maintenance, security, or transitive-dependency audit was performed.

| Package | Installed version | PyPI license expression | Runtime/dev |
| --- | --- | --- | --- |
| fastapi | 0.148.0 | MIT | runtime |
| uvicorn | 0.54.0 | BSD-3-Clause | runtime |
| pydantic | 2.14.1 | MIT | runtime |
| pydantic-settings | 2.16.0 | MIT | runtime |
| sqlalchemy | 2.1.5 | MIT | runtime |
| alembic | 1.20.0 | MIT | runtime |
| typer | 0.27.3 | MIT | runtime |
| httpx | 0.28.1 | BSD-3-Clause | runtime |
| python-json-logger | 4.2.0 | BSD-2-Clause | runtime |
| pytest | 9.1.1 | MIT | dev |
| pytest-cov | 7.1.0 | MIT | dev |
| hypothesis | 6.170.2 | MPL-2.0 | dev |
| ruff | 0.17.1 | MIT | dev |
| mypy | 2.4.0 | MIT | dev |
| types-requests | 2.33.0.20261006 | Apache-2.0 | dev |

Dependency maturity and vulnerability review is **not complete**. The repository must complete the
SPEC §12 adoption checklist before adding any integration runtime dependency and before external
distribution. No repository shortlist candidate (OPA, OpenTelemetry, Inspect AI, Promptfoo, Ragas,
DeepEval, garak, in-toto, Sigstore, Semgrep, Trivy, OSV-Scanner, etc.) is installed or has been
inspected.

### 1.3 Source tree

`src/nyx/__init__.py` is the only Python file under `src/`. All other module directories exist but are
empty:

```text
src/nyx/
├── __init__.py
├── adapters/
├── api/
├── audit/
├── cli/
├── critics/
├── disputes/
├── domain/
├── evidence/
├── findings/
├── ingestion/
├── observability/
├── policies/
└── storage/
```

There is no `src/nyx/systems/` directory. System registration is therefore not yet assigned to a
top-level package; `docs/architecture.md` assigns that capability to `domain`, `storage`, and `api`
rather than inventing an empty package.

### 1.4 Tests

The only test file is `tests/unit/test_smoke.py`. It asserts that `import nyx` succeeds. The test
directories `tests/contract`, `tests/e2e`, `tests/fixtures`, `tests/integration`, and
`tests/security` exist but contain no test files.

Observed commands and results at inspection time:

```text
$ uv run pytest -q
.                                                                        [100%]

$ uv run ruff check .
All checks passed!

$ uv run mypy
Success: no issues found in 1 source file
```

### 1.5 Documents, schemas, examples, migrations

| Path | Observed state |
| --- | --- |
| `docs/SPEC.md` | Present; full contract |
| `docs/architecture.md` | Did not exist before this run |
| `docs/threat-model.md` | Did not exist before this run |
| `docs/evidence-model.md` | Did not exist before this run |
| `docs/adr/` | Empty before this run |
| `docs/implementation/` | Empty before this run |
| `schemas/` | Empty before this run |
| `examples/` | Empty before this run |
| `migrations/` | Empty; no `alembic.ini` and no Alembic environment |
| `.github/workflows/` | Empty |

### 1.6 CLI status

The console script is declared but its target does not exist. Running it fails:

```text
$ uv run nyx --help
Traceback (most recent call last):
  ...
ModuleNotFoundError: No module named 'nyx.cli.main'
exit=1
```

The README quick-start instructions (`uv run alembic upgrade head`, `uv run nyx doctor`) therefore
cannot succeed yet.

## 2. What is reusable

| Asset | Reuse in Phase 1 |
| --- | --- |
| `pyproject.toml` dependency set | Reuse as-is for the Phase 1 vertical slice: FastAPI, Pydantic v2, SQLAlchemy, Alembic, Typer, pytest, Hypothesis |
| `src/nyx` package + Hatchling layout | Extend in place; no packaging change needed |
| Empty module directories | Fill the existing package boundaries; do not rename or restructure without an ADR |
| pytest configuration | Reuse `pythonpath = ["src"]` and `testpaths = ["tests"]` |
| Ruff and mypy configuration | Reuse strict mypy; add modules, do not relax settings |
| `uv.lock` | Keep lockfile updates with dependency changes |
| `tests/unit/test_smoke.py` | Keep as the import smoke test |
| `AGENTS.md` | Binding operating rules; unchanged |
| `docs/SPEC.md` | Authoritative contract; derived docs must not contradict it |

## 3. What is missing

Facts about absence, stated without assumptions about future code:

- No domain models, Pydantic schemas, or database tables.
- No SQLAlchemy engine, session factory, repositories, or transaction helper.
- No Alembic configuration, environment, or migration.
- No FastAPI application, routers, error handlers, authentication, or authorisation.
- No CLI implementation, so the declared `nyx` entry point is broken.
- No ingestion pipeline, redaction, canonicalisation, hashing, idempotency, or deduplication.
- No critic interface, critic registry, deterministic critics, or C-001 implementation.
- No finding composition, lifecycle state machine, or query surface.
- No rebuttal, verification, disposition, or history workflow.
- No audit log or audit verification.
- No policy engine or policy catalogue implementation.
- No adapters for Git/CI, OpenTelemetry, OPA, or any ecosystem project.
- No observability configuration or metrics.
- No fixtures, demo data, or end-to-end demonstration.
- No API contract, OpenAPI document, or CLI contract tests.
- No automated schema/model agreement test.
- No dependency or container scanning workflow.

## 4. Constraints carried into Phase 1

- The existing stack and package layout are retained; this is not a rewrite.
- Phase 1 must deliver one deployable service, not microservices (SPEC §4, ADR 0002).
- The `nyx` entry point must be repaired by creating `nyx.cli.main:app` rather than changing the
  declared script target.
- JSON Schemas and Pydantic models must be kept in agreement; a contract test is required in Phase 1.
- No claim of verification may be made for any behaviour not exercised by a command that was actually
  run.
