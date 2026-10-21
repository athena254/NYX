# Phase 5 plan — Integrations

**Source:** `docs/SPEC.md` §4.1 (`adapters`), §12, §14, §15, §17.1, §19 (Phase 5), §20 step 15, §21;
`docs/architecture.md` §2 (`adapters/`), §3 (dependency direction), §4 (separation of concerns);
`docs/threat-model.md`; ADR 0004 (no external action), ADR 0006 (untrusted content);
`docs/implementation/roadmap.md` §Phase 5; `docs/implementation/task-checklist.md` steps 15–16.
**Owner of this plan:** the orchestrating agent. **Status:** proposed, not yet approved.
**Phase 5 exit criterion (SPEC §19):** *adapters can be disabled independently and core NYX still
works.* §14 item 2 names the Git/CI adapter first; this plan starts there.

**Last verified against the code:** 22 October 2026, branch `master`, HEAD `a76c8da`. Gates run on the
working tree at that commit: `uv run pytest` → **552 passed, 1 skipped**; `uv run mypy` and
`uv run ruff check .` are Phase 4-clean at that commit; `uv audit` → **no known vulnerabilities in
70 packages** (the Phase 4 finding that `certifi` carried 2 CVEs is fixed at `certifi 2026.10.19`).
**Working tree is not clean** — see §10, finding D6.

Every claim about existing code below was read, not inferred from a filename.

---

## 0. Ground rules this plan does not restate lightly

- **Append-only.** No update or delete path for findings, rebuttals, dispositions, verification jobs,
  or policy versions. An adapter ingests; it never edits a record it already wrote.
- **All state changes go through `FindingLifecycle`.** An adapter never assigns `finding.status`.
- **Never hardcode an actor or principal.** The adapter records the *external* producer identity it
  observed (`producer_identity` on evidence, `actor` on events) and passes its own service identity
  as the NYX-side actor, exactly as `src/nyx/ingestion/gateway.py:100-102` does for the ingestion
  gateway. It does not invent an operator.
- **Secrets from the environment / `.env` only.** A PAT is read from a named environment variable at
  client construction. It is never written to `MonitoredSystem.metadata` — the domain validator
  already rejects secret-looking metadata keys (`src/nyx/domain/base.py:132-164`,
  `src/nyx/domain/systems.py:57-65`).
- **Adapters are read-only observers.** ADR 0004: "Adapters in the MVP are read-only observers. They
  do not expose mutation capabilities." The Git/CI adapter calls `GET` endpoints only.
- **Never vendor another project's code.** Siblings are consumed over HTTP. Sibling *patterns* may be
  ported; sibling *source* may not be copied, and NYX's source may not be copied into them.
- **A delegation slice must not claim verification it did not perform.** Contract tests must be run
  and their output reported.

---

## 1. SPEC §12 dependency / license / maturity review (the Phase 0 open item)

SPEC §12 lists the fields a dependency review must record. `docs/implementation/roadmap.md` §Phase 0
leaves exactly one box open: *"Complete the dependency/license/maturity review in SPEC §12 before
adding any integration dependency. Installed versions and licenses are recorded in
`current-state.md`; no independent security or maturity audit has been performed."*

**Verdict: Phase 5 needs ZERO new third-party runtime dependencies.** The Git/CI adapter is built on
the standard library plus `httpx`, which is already a runtime dependency
(`pyproject.toml:15`, used by `src/nyx/conversational/client.py`, `llm.py`, `telegram/adapter.py`).

### 1.1 Packages already installed (baseline, read from the venv, not assumed)

| Package | Installed | License (from installed metadata) | Maturity / maintenance | Needed for Phase 5? |
| --- | --- | --- | --- | --- |
| `fastapi` | 0.148.0 | MIT | Production/stable, active | No (already there) |
| `uvicorn[standard]` | 0.54.0 | BSD-3-Clause | Active | No |
| `pydantic` | 2.14.1 | MIT | Active | No |
| `pydantic-settings` | 2.16.0 | MIT | Active | No |
| `sqlalchemy` | 2.1.5 | MIT | Active | No |
| `alembic` | 1.20.0 | MIT | Active | No |
| `typer` | 0.27.3 | MIT | Active | No |
| **`httpx`** | **0.28.1** | **BSD-3-Clause** | **Active; the project's HTTP client since Phase 7** | **Yes — the Git/CI transport** |
| `python-json-logger` | 4.2.0 | BSD-2-Clause | Active | No |
| `jsonschema` | 4.26.0 | MIT | Active (already a runtime dep, `pyproject.toml:19`) | No |
| `hypothesis` | 6.170.2 | MPL-2.0 | Active | No |
| `pytest` / `pytest-cov` / `ruff` / `mypy` | 9.1.1 / 7.1.0 / 0.17.1 / 2.4.0 | MIT | Active | No (dev) |
| `mcp` (extra) | 2.3.0 | MIT | Active; optional extra only | No |
| `opentelemetry-api` | 1.45.1 | Apache-2.0 | Transitive of `fastapi` 0.148.0 and `mcp` 2.3.0; **no NYX module imports it** | No — see §1.3 |
| `certifi` | 2026.10.19 | MPL-2.0 | `uv audit` clean at this version | No |

`uv pip list` was used for versions; license strings were read from each installed distribution's
metadata on this machine. `current-state.md` §1.2 records the same values from PyPI metadata on
19 October 2026 and flags that no independent audit was done then. This table *is* that audit, scoped
to what Phase 5 would add.

### 1.2 Candidates considered and rejected for the Git/CI adapter

Each is recorded with the SPEC §12 fields that decided it, so the rejection is auditable rather than
silent.

| Candidate | Upstream / license | Latest inspected | Maturity | Rejection reason (SPEC §12 field) |
| --- | --- | --- | --- | --- |
| **`PyGithub`** | `PyGithub/PyGithub` — **LGPL-3.0** | 2.10.0, 20 Aug 2026 | Production/Stable, 4 maintainers, Snyk "Sustainable"; **the README states it is actively seeking maintainers** | **License + smaller-adapter field.** LGPL in a service NYX may distribute; and it is a general GitHub object model, not the four read endpoints this adapter needs. Rejected in favour of stdlib `json` + `httpx`. |
| **`pygit2`** | `libgit2/pygit2` — **GPL-2.0 with linking exception** | 1.20.1, 12 Sep 2026 | Production/Stable, healthy cadence, **1 maintainer** | **License + transitive risk.** GPL-2.0 (even with the linking exception) is a worse fit than LGPL for a service, it bundles `libgit2`, and the local-git slice does not need a C-backed object model. Rejected. |
| **`dulwich`** | `dulwich` — Apache-2.0 **or** GPL-2.0 | 1.2.17, 3 Oct 2026 | Active, 1 maintainer | **Smaller-adapter field.** Pure-Python git is attractive, but the local-git slice can be `git` **CLI** (already the repo's own tool, `git` is how NYX itself is developed) behind the same adapter protocol. A library is not needed to shell out, and `subprocess` is forbidden in `src/` (§5, gate G7). Rejected. |
| **`GitPython`** | `gitpython` — BSD-3-Clause | not inspected | not inspected | **Not inspected → not adoptable.** SPEC §12 forbids selecting on a README. If a later slice wants a local-git *library*, inspect it first. |
| **`requests`** | `psf/requests` — Apache-2.0 | not installed | n/a | **Already the wrong tool for this repo.** `httpx` is a declared runtime dependency; adding a second HTTP client is the duplicated-platform mistake SPEC §3 warns about. Rejected. |
| **OPA / `in-toto` / Sigstore / Semgrep / Trivy / OSV-Scanner** | various | not installed | various | **Not Phase 5's Git/CI slice.** They belong to SPEC §14 items 5–6 (policy, provenance) and to
SPEC Part I Phase 4/5 in the research plan, which is a *different* numbering from Part II §19.
SPEC §12 applies to each when its slice is proposed. Recorded here as **deferred, not decided**. |
| **OpenTelemetry SDK / OpenInference** | Apache-2.0 | not installed | active | **Deferred.** `opentelemetry-api` is present only as a transitive of `fastapi`/`mcp`; the *SDK* and
the OTLP ingest path are a later slice (§9, O5). Adding an exporter now would be a second ingestion
path before the first adapter is proven. |

### 1.3 Explicit statement required by the task

> **No new third-party dependency is required for Phase 5.** The Git/CI adapter uses `httpx`
> (already installed, BSD-3-Clause, already the project's HTTP client) and the standard library
> (`json`, `os`, `re`, `datetime`, `dataclasses`, `typing`). No package is added to
> `pyproject.toml` by any slice in §4. If a later slice needs one, it must repeat this table against
> SPEC §12 first, and the default answer must remain "no".

---

## 2. The adapter contract

### 2.1 Where the boundary is today

`src/nyx/adapters/` **exists and is empty** — no `__init__.py`, no module, and no commit in history
touches it (`git log --all -- src/nyx/adapters` returns nothing; `git ls-files src/nyx/adapters`
returns nothing). `docs/architecture.md` §2 sketches an `Adapter` protocol with `adapter_id`,
`version`, `health()` and `normalize()` returning `list[EventInput | ClaimInput | EvidenceInput]`,
and §3 states the one rule that matters: **`adapters` may import `domain` and ingestion input types;
core modules never import `adapters`.**

The existing boundary a new adapter must sit behind is the **ingestion gateway**, not the API:

```
GitHub API / local git repo
        |  (httpx GET, or `git` CLI output, or a JSON fixture file)
        v
src/nyx/adapters/git_ci/        <-- Phase 5: normalize ONLY
        |  EventInput | ClaimInput | EvidenceInput
        v
IngestionGateway.ingest_event / ingest_claim / ingest_evidence
        |  size limit -> decode -> validate -> redact -> dedupe -> persist + audit
        v
domain + storage                <-- unchanged
```

This is the same boundary every other producer already uses (`src/nyx/ingestion/service.py`,
`src/nyx/api/routes/events.py`, the CLI). An adapter therefore inherits, for free:

- **Redaction before persistence** (`gateway.py:277-282`) — a CI log line that happens to contain a
  token never reaches the database.
- **Schema validation and the typed error envelope** (`gateway.py:158-175`) — a malformed GitHub
  payload is `SCHEMA_VALIDATION_FAILED` with field pointers, not a traceback.
- **Identity deduplication** (`ingestion/idempotency.py`) — a re-fetched run is a duplicate, not a
  second finding. The adapter sets `producer_event_id` from the external object's own id.
- **Audit entries** for every accepted record (`gateway.py:349-359`).
- **The capability seam** — an adapter that runs inside the HTTP surface is subject to the same
  `Capability` checks as any other route.

### 2.2 The exact interface

`docs/architecture.md` §2 names `Adapter`, `AdapterHealth`, `AdapterDescriptor`, `AdapterRegistry`.
That sketch is not implemented anywhere. **Slice 5.1 implements it as the contract every adapter
must satisfy.** It is deliberately a `Protocol`, so an adapter is a class with these attributes and
methods and nothing else — no storage, no session, no `Session` import.

```python
# src/nyx/adapters/base.py  (slice 5.0)

class AdapterState(StrEnum):
    UNKNOWN = "unknown"
    OK = "ok"
    DEGRADED = "degraded"
    DISABLED = "disabled"

class AdapterHealth(NyxModel):
    """What a health probe established. ``detail`` must never carry a credential."""
    adapter_id: Identifier
    version: str
    state: AdapterState
    detail: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    checked_at: UtcDatetime

class AdapterDescriptor(NyxModel):
    """The public description of one registered adapter."""
    adapter_id: Identifier
    version: str
    description: str
    permissions: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]]
    # What it can and cannot observe, so a ``no observation`` result is interpretable.
    can_observe: list[str]
    cannot_observe: list[str]

@runtime_checkable
class Adapter(Protocol):
    """The structural contract every Phase 5 adapter implements."""
    adapter_id: str
    version: str
    description: str

    def health(self) -> AdapterHealth: ...
    def normalize(self, raw: object) -> list[EventInput | ClaimInput | EvidenceInput]: ...

class AdapterRegistry:
    def register(self, adapter: Adapter) -> None: ...
    def get(self, adapter_id: str) -> Adapter: ...
    def enabled(self) -> list[AdapterDescriptor]: ...
    def states(self) -> list[AdapterHealth]: ...
```

Two design decisions, both forced by existing code:

1. **`normalize` returns producer *input* models, not domain models.** `EventInput`, `ClaimInput`
   and `EvidenceInput` (`src/nyx/ingestion/inputs.py`) are the shapes the gateway validates and
   redacts. Returning domain models would skip redaction and validation, which is the one thing the
   boundary exists to guarantee.
2. **`health` takes no arguments and reads no network.** A health probe that fetches is a health
   probe that can hang. The Git/CI adapter's `health` performs one bounded `GET` with a short
   timeout and reports `DEGRADED` on any failure — it never raises.

### 2.3 The Git/CI adapter's own surface

```python
# src/nyx/adapters/git_ci/config.py   (slice 5.1)
class GitCiConfig(BaseModel):
    """Validated adapter configuration. No secret field: the token is read from the environment."""
    provider: Literal["github", "local_git", "fixture"]
    api_base_url: str                      # https://api.github.com for the GitHub provider
    repository: Annotated[str, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"]
    token_env: str | None = None           # NAME of the env var holding the PAT; never the value
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    max_runs_per_fetch: int = Field(default=10, ge=1, le=100)
    allow_private_targets: bool = False    # mirrors evidence_allow_private_urls; default safe

# src/nyx/adapters/git_ci/github_client.py   (slice 5.1)
class GitCiError(Exception):
    """A fetch or normalization failure. Carries a stable ``code``; never carries the token."""
    code: str   # "unavailable" | "unauthorized" | "not_found" | "invalid_output" | "denied"

class GitHubCiClient:
    def __init__(self, config: GitCiConfig, *, transport: httpx.BaseTransport | None = None) -> None: ...
    def fetch_check_runs(self, *, ref: str) -> list[dict[str, Any]]: ...   # GET /repos/{repo}/commits/{ref}/check-runs
    def fetch_actions_runs(self, *, ref: str) -> list[dict[str, Any]]: ... # GET /repos/{repo}/actions/runs

# src/nyx/adapters/git_ci/normalize.py   (slice 5.2)
@dataclass(frozen=True)
class NormalizedCiRecord:
    """One CI observation before it becomes NYX input, with the external identity preserved."""
    producer_event_id: str
    event_type: str            # ci.check.completed | ci.workflow.completed | commit.observed
    occurred_at: datetime
    actor: PrincipalRef | None
    scope: Scope
    payload: dict[str, Any]
    source_ref: str
    trust_class: TrustClass
    external_run_id: str

def normalize_check_run(run: dict[str, Any], *, repository: str) -> list[EventInput | EvidenceInput]: ...
def normalize_actions_run(run: dict[str, Any], *, repository: str) -> list[EventInput | EvidenceInput]: ...
def normalize_commits(commits: list[dict[str, Any]], *, repository: str) -> list[EventInput]: ...

# src/nyx/adapters/git_ci/adapter.py   (slice 5.3)
class GitCiAdapter:
    adapter_id = "git-ci"
    version = "1.0.0"
    description = "Observes GitHub check runs, Actions runs, and commits as NYX events and CI evidence."
    def __init__(self, config: GitCiConfig, *, client: GitHubCiClient | None = None) -> None: ...
    def health(self) -> AdapterHealth: ...
    def normalize(self, raw: object) -> list[EventInput | ClaimInput | EvidenceInput]: ...
    def fetch_for_ref(self, ref: str) -> list[EventInput | ClaimInput | EvidenceInput]: ...
```

`adapter_id` is `"git-ci"`, which is what `NYX_ENABLED_ADAPTERS=git-ci` names.

### 2.4 What the adapter maps to in NYX's domain

| External thing | NYX record | Concrete field mapping |
| --- | --- | --- |
| Repository `owner/name` | **`MonitoredSystem`** | `name` = repository, `kind` = `repository`, `environment` = `staging`/`production` from config, `owner` = the repo owner, `adapter` = `http`, `capabilities` = `["events.write", "findings.read"]`. `metadata` carries **no** token and no `api_base_url` credential. |
| Commit SHA | **`Scope.commit`** | Set on the evidence's `Scope` — this is the field C-001 compares (`c001_completion_vs_verification.py:108,347-352`). **The event carries it in `payload["commit"]` instead:** `EventInput` (`src/nyx/ingestion/inputs.py:66-88`) has no `scope` field, so the plan's original wording was not literally implementable. Corrected here. |
| Workflow / check-run id | **`Event.idempotency` producer key** | `producer_event_id` = `"gh:{owner}/{repo}:check:{id}"` or `"gh:{owner}/{repo}:run:{id}"`. Stable across refetches, so a re-run is `DUPLICATE_EVENT`, not a second record. |
| Check run / Actions run | **`Event`** | `event_type` = `ci.check.completed` or `ci.workflow.completed` (matches `EventType`'s `^[a-z0-9]+(\.[a-z0-9][a-z0-9_-]*)+$`), `correlation_id` = the external run id, `actor` = the GitHub actor (`PrincipalRef`, `kind=tool`), `trust_class` = `observed` (a CI record establishes the recorded event, not the truth of the claim — SPEC §6). |
| Run conclusion (`success`/`failure`/`neutral`/`cancelled`) | **`EvidenceItem`** | `kind` = `ci_test_result`, `title` = the job name, `description` = the conclusion + suite, `source_ref` = the run's `html_url`, `locator` = `job:{name}`, `integrity_method` = `hash`, `scope.commit` = the run's head SHA, `sensitivity` = `internal`, `access_policy_ref` = `evidence:internal` (a known value — see the boundary note below), `retention_class` = `standard`. |
| PR merge / review decision | **`EvidenceItem`** | `kind` = `review_record` (already in `EvidenceKind`). |
| Artifact digest | **`EvidenceItem`** | `kind` = `artifact_digest`, `content_hash` set to the digest the host reported. |
| **Critic output** | **nothing** | The adapter never creates a finding or a disposition. It produces *inputs*: an event and an evidence item per run. **It does not emit a claim** — a claim synthesized from the same CI run would make C-001 assess a claim against evidence from its own source, which SPEC §8 warns is not independent corroboration. |

**Why this is the right mapping.** C-001 already compares a claim's `scope.commit` against the
`scope.commit` of linked `test_result` / `ci_test_result` evidence, and already reads a pass/fail
word from the evidence title and description (`c001_completion_vs_verification.py:41-53`, with the
limitation documented in that module's docstring). The Git/CI adapter therefore makes the existing
critic *better fed*, not redundant: it turns "a producer hand-wrote a CI fixture" into "NYX fetched
the run itself". It does not change what C-001 concludes.

**Boundary note — `access_policy_ref`.** The evidence route treats `evidence:*` values as strict and
refuses an unknown one at registration (`src/nyx/api/routes/evidence.py:36-51`). The adapter must
emit one of `evidence:read`, `evidence:internal`, `evidence:public`, `evidence:standard`, or
`evidence:restricted` — never a free-form value such as the `policy:evidence-internal-read` that the
existing demo fixture uses (`tests/fixtures/evidence_test_def456.json:34`). The demo fixture is a
pre-Phase-4 file and is not a contract; the adapter follows the route.

**Trust class.** CI records are `observed`, never `tool_verified` or `signed_attestation`. A GitHub
API response establishes that GitHub recorded the run; it does not establish that the pipeline was
not influenced by the same actor whose claim is under assessment (SPEC §8, "correlated sources").

### 2.5 How it stays behind the same boundary as every other adapter

1. **It imports only `domain` and `ingestion.inputs`** (plus `httpx`, `pydantic`, `json`, `re`). It
   imports nothing from `critics`, `findings`, `disputes`, `storage`, `policies`, or `retention`.
   Enforced by gate **G7** in §5.
2. **It is not imported by core.** `src/nyx/api/app.py` and `src/nyx/cli/main.py` are
   orchestrator-only; they import the adapter to *construct* it, never the reverse. Enforced by the
   same gate.
3. **It performs no external action.** Read-only `GET` requests; no `POST`/`PATCH`/`DELETE`; no
   branch creation, no PR, no status write, no deploy. ADR 0004.
4. **It is disabled by default and independently** (§6).
5. **It is a `Protocol` implementation, not a base class.** Nothing in `domain` knows it exists.

---

## 3. Slices

Six slices. **File ownership is exclusive** — no two slices create or modify the same file. Where a
slice would need a shared file, the orchestrator owns that edit and lands it between waves. This is
the Phase 4 rule (`docs/implementation/phase-4-plan.md` §4) applied again.

`src/nyx/api/app.py` and `src/nyx/cli/main.py` are **orchestrator-only**, as in Phase 4. A delegated
slice delivers modules and tests; the orchestrator wires them.

| Slice | Deliverable | Wave |
| --- | --- | --- |
| **5.0 Shared adapter infrastructure** (orchestrator) | `Adapter` protocol, registry, health, config fields | 0 |
| **5.1 GitHub CI client + config** | bounded HTTP fetch, no secrets in output | 1 |
| **5.2 Normalization** | GitHub JSON → `EventInput`/`EvidenceInput` | 1 |
| **5.3 Adapter assembly + live contract test** | `GitCiAdapter`, end-to-end over a fake HTTP server | 2 |
| **5.4 Local-git provider** | same contract over a real local repository | 2 |
| **5.5 Fixture provider + disablement test** | offline provider; the exit criterion | 3 |

**Concurrency limit: 2 coding agents at once.** This machine has 2 cores and measured ~725 MB free
RAM in Phase 4; each agent process holds 300–900 MB. Wave 1 runs 5.1 + 5.2 in parallel; wave 2 runs
5.3 + 5.4; wave 3 is 5.5 alone. Exceeding this produces silent stalls, not speed.

**Orchestrator-owned edits, landed before any child starts:** the `Settings` fields in
`src/nyx/config.py`, the mirror block in `.env.example`, and any new `ErrorCode` member in
`src/nyx/error_codes.py`. A child that starts before those land can create a file the orchestrator
then cannot write.

---

### Slice 5.0 — shared adapter infrastructure (orchestrator)

**Status: landed, commit `c0bc402`.** Gates: `uv run pytest` 565 passed / 1 skipped · `uv run mypy`
clean (122 files) · `uv run ruff check .` clean · `uv run ruff format --check .` 198 files ·
`uv run alembic upgrade head` clean · `docs/openapi.json` unchanged.

Four deliberate deviations from §2 are recorded below.

**Deliverables**

```python
# src/nyx/adapters/__init__.py
# exports: Adapter, AdapterDescriptor, AdapterHealth, AdapterRegistry, AdapterState

# src/nyx/adapters/base.py
class AdapterState(StrEnum): ...            # §2.2
class AdapterHealth(NyxModel): ...
class AdapterDescriptor(NyxModel): ...
@runtime_checkable
class Adapter(Protocol): ...
class AdapterRegistry:
    def register(self, adapter: Adapter) -> None: ...
    def get(self, adapter_id: str) -> Adapter: ...
    def enabled(self) -> list[AdapterDescriptor]: ...
    def states(self) -> list[AdapterHealth]: ...
```

`AdapterRegistry.enabled()` returns descriptors for every registered adapter, **not** only the ones
in `Settings.enabled_adapters`. The settings list is the *deployment* filter; the registry is the
*code* inventory. Keeping them apart is what lets `nyx doctor` say "registered but disabled" without
lying (the Phase 4 defect #7 was exactly a doctor check that reported configuration as availability).

`AdapterRegistry` holds no session and no network. `states()` calls each adapter's `health()` and
catches everything: an adapter that raises is `DEGRADED` with the exception *type name* only, never
its message (a message can echo a URL with a credential).

**Add to `src/nyx/config.py`** (orchestrator; a child must not edit this file):

```python
# Phase 5: optional adapters. Every adapter is off by default; enabling one is an explicit
# deployment decision and never a prerequisite for core NYX.
adapter_enabled: dict[str, bool] = Field(default_factory=dict)
# Git/CI adapter. The token is read from the named environment variable at client construction;
# it is never a setting value and never enters effective configuration.
git_ci_provider: Literal["github", "local_git", "fixture"] = "github"
git_ci_api_base_url: str = "https://api.github.com"
git_ci_repository: str = ""
git_ci_token_env: str | None = None
git_ci_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
git_ci_max_runs_per_fetch: int = Field(default=10, ge=1, le=100)
git_ci_allow_private_targets: bool = False
```

Validators (fail closed, naming the variable):

- `adapter_enabled["git-ci"] is True` with `git_ci_provider == "github"` and **no**
  `git_ci_token_env` → `ValueError` naming both. A GitHub adapter with no token source is not a
  degraded adapter, it is a misconfiguration; the private repo simply cannot be read.
- `git_ci_repository` empty when `git-ci` is enabled → `ValueError`. An adapter with no target
  repository has nothing to observe.
- `git_ci_timeout_seconds` bounds come from `Field`.
- `git_ci_allow_private_targets=True` requires `evidence_allow_private_urls=True` as well, or it is
  refused: two switches that must agree, or the SSRF guard and the adapter disagree about what is
  fetchable.

**Mirror every variable in `.env.example`** with the safe default and the failure mode stated. Never
put a real PAT in the file; name the variable that holds it.

**`ErrorCode` additions** (orchestrator; the catalogue is a shared file and codes are never renamed):
`ADAPTER_DISABLED = "ADAPTER_DISABLED"` (409, not retryable) and
`ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"` (503, retryable). `DEPENDENCY_UNAVAILABLE` already
exists and is *not* reused: an optional adapter being down is not a required dependency being down,
and conflating them is how a disabled adapter gets reported as an outage.

**Add to `docs/adr/`:** `0008-adapter-boundary-and-disablement.md`. It must record the two
decisions that are easy to get wrong: (a) an adapter is a *producer* behind the ingestion gateway,
not a new ingestion path; (b) "disabled" means the adapter is not constructed, not that it is
constructed and ignored — a constructed adapter that is ignored is a health probe that can still
hang a request.

**Tests — `tests/contract/test_adapter_registry.py`:** registration replaces on the same id;
`enabled()` returns descriptors in stable id order; `states()` reports `DEGRADED` (not a raise) for
an adapter whose `health()` raises; `states()` never includes an exception message; an adapter that
satisfies `Adapter` structurally is accepted and one missing `normalize` is not (assert with
`isinstance(adapter, Adapter)` and `runtime_checkable`).

**EXCLUSIVE files this slice may create or modify**

- `src/nyx/adapters/__init__.py` (new)
- `src/nyx/adapters/base.py` (new)
- `src/nyx/config.py`
- `.env.example`
- `src/nyx/error_codes.py`
- `docs/adr/0008-adapter-boundary-and-disablement.md` (new)
- `tests/contract/test_adapter_registry.py` (new)
- `tests/contract/test_adapter_boundary.py` (new) — gate G7

**As-built deviations from §2, all deliberate**

1. **`enabled_adapters: list[str]` is the deployment filter, not a new `adapter_enabled: dict[str, bool]`.**
   `Settings` already carried `enabled_adapters` from Phase 1b. A second setting for the same
   decision is two sources of truth, so the dict was dropped. `AdapterRegistry(is_enabled=...)`
   is injected from it.
2. **`Adapter` carries `permissions`, `can_observe`, and `cannot_observe` as attributes.** §2.2
   listed them only on `AdapterDescriptor`, but the descriptor has to be built *from* the
   adapter, so the adapter must declare them. mypy strict enforces it structurally.
3. **`registered()` was added, and `enabled()` filters.** §2.2 had only `enabled()`. Reporting
   "registered but disabled" needs both, and §6.1 uses both terms.
4. **`states()` probes only enabled adapters.** A disabled adapter is not constructed, so there
   is nothing to probe — and probing one would mean constructing it to hang a health check on it.

**FORBIDDEN files** (touching any of these is a collision and a failed slice)

- `src/nyx/api/app.py`, `src/nyx/cli/main.py` — orchestrator-only
- `src/nyx/adapters/git_ci/**` — slice 5.1–5.4
- `tests/contract/test_git_ci_adapter.py` — slice 5.3
- `tests/unit/test_git_ci_normalize.py` — slice 5.2
- `tests/unit/test_git_ci_local_git.py` — slice 5.4
- `tests/integration/test_adapter_disablement.py` — slice 5.5
- `pyproject.toml`, `uv.lock`, `migrations/**`, `schemas/**`, `docs/openapi.json`
- `docs/implementation/phase-5-plan.md` — the orchestrator's own document
- Any file under `src/nyx/{critics,findings,disputes,policies,storage,retention,auth,audit,systems,observability,ingestion}/`

**Gate commands, in order**

```bash
uv run pytest tests/contract/test_adapter_registry.py
uv run pytest
uv run mypy
uv run ruff check .
uv run ruff format --check .
uv run alembic upgrade head          # from a CLEAN database
```

Read `${PIPESTATUS[0]}` or drop the pipe. A red gate is a defect in the slice, not a note.

**Live-agent warning.** A delegated agent on this slice is building a *contract*, so the temptation
is to make `Adapter` an abstract base class with a `normalize` that returns domain models. **Do not.**
Domain models bypass redaction and validation; the whole point of the boundary is that an adapter's
output goes through the same pipeline as a hand-written fixture. If the agent finds itself importing
`nyx.storage` or `nyx.findings` inside `src/nyx/adapters/`, it has broken the boundary — stop and
report it rather than "fixing" it with a session parameter.

---

### Slice 5.1 — GitHub CI client and configuration

**Status: landed.** `GitHubCiClient` (233 lines) + `GitCiConfig` (61) + `__init__` + 8 tests.
Independently probed over a real socket by `tmp/probes/probe_git_ci_adapter.py`: the token
reaches the stub server in the `Authorization` header and nowhere else, no query string is sent,
a 401 is `unauthorized` with the token absent from the message, and a private host is refused
with zero requests recorded. Boundary grep (G7) and the security allowlist gate both pass.

**Deliverables**

- `src/nyx/adapters/git_ci/__init__.py` — exports `GitCiConfig`, `GitCiError`, `GitHubCiClient`.
- `src/nyx/adapters/git_ci/config.py` — `GitCiConfig` exactly as in §2.3, plus a `from_settings`
  classmethod that reads the `git_ci_*` fields added in slice 5.0 and the token from
  `os.environ[config.token_env]` **at construction**, never storing the value.
- `src/nyx/adapters/git_ci/github_client.py` — `GitHubCiClient` with `fetch_check_runs(ref)` and
  `fetch_actions_runs(ref)`.

**The client's hard rules**

1. **`GET` only.** The class exposes no method that writes. ADR 0004.
2. **The token never appears in an exception, a log, or a return value.** It is placed in an
   `Authorization` header built once in `__init__`; every failure path raises `GitCiError` with a
   message composed from the HTTP status and the *URL without its query string*, never from
   `str(exc)` of an `httpx` error that might carry the full request. This is the exact shape of the
   defect `HANDOFF.md` §7 records (a password in `base_url` leaving in a Basic-auth header no test
   inspected) — the fix here is that the token is in a header the client builds, and the client's
   own tests assert the header is the only place it appears.
3. **`transport` is injectable**, so the contract test can point the client at a local `httpx`
   transport with no network. Same pattern as `src/nyx/conversational/client.py:53`.
4. **Timeout is enforced by `httpx`**, from `config.timeout_seconds`. A client that can hang is a
   client that can hang a request.
5. **`allow_private_targets` is honoured.** When `False` (the default) the client resolves the host
   through `nyx.ingestion.url_fetch.is_private_target` and refuses before the request — the same
   guard the evidence route applies at the ingestion edge (`gateway.py:486-507`). A GitHub host is
   public, but the guard is structural and must not depend on that being true tomorrow.
6. **`max_runs_per_fetch` bounds the response.** A runaway pagination loop is a resource-exhaustion
   bug in a service that is supposed to be locally deployable.
7. **No `subprocess`, no `os.system`, no `eval(`, no `exec(`** — gate G7.

**Tests — `tests/unit/test_git_ci_client.py`**

1. Positive: a fake transport returns a check-runs payload; `fetch_check_runs` returns the parsed
   list and made exactly one `GET` to `/repos/{owner}/{repo}/commits/{ref}/check-runs`.
2. Negative: HTTP 401 → `GitCiError` with code `unauthorized`, and the message contains no token.
3. Negative: HTTP 404 → code `not_found`. HTTP 429/5xx → code `unavailable`.
4. Negative: a `httpx.TimeoutException` → code `unavailable`, no raise.
5. **Secret-hygiene (the one that matters):** the token is absent from `str(exc)`, from
   `repr(client)`, from every attribute of `client`, and from the `GitCiError` message. Assert by
   putting the token *only* in the input (an env var) and searching every output for it — the
   corrected-probe discipline in `HANDOFF.md` §7. Do not build the search string from the token.
6. Negative: a host that `is_private_target` reports as private is refused before any request is
   made (assert the fake transport recorded zero requests).
7. Boundary: `max_runs_per_fetch` truncates a long response and the truncation is reported in the
   returned metadata, not silent.

**EXCLUSIVE files this slice may create or modify**

- `src/nyx/adapters/git_ci/__init__.py` (new)
- `src/nyx/adapters/git_ci/config.py` (new)
- `src/nyx/adapters/git_ci/github_client.py` (new)
- `tests/unit/test_git_ci_client.py` (new)

**FORBIDDEN files**

- `src/nyx/adapters/base.py`, `src/nyx/adapters/__init__.py` — slice 5.0
- `src/nyx/adapters/git_ci/normalize.py`, `src/nyx/adapters/git_ci/adapter.py`,
  `src/nyx/adapters/git_ci/local_git.py` — slices 5.2, 5.3, 5.4
- `src/nyx/config.py`, `.env.example`, `src/nyx/error_codes.py` — slice 5.0
- `src/nyx/api/app.py`, `src/nyx/cli/main.py`
- `tests/contract/test_git_ci_adapter.py`, `tests/unit/test_git_ci_normalize.py`,
  `tests/unit/test_git_ci_local_git.py`, `tests/integration/test_adapter_disablement.py`
- `pyproject.toml`, `uv.lock`, `migrations/**`, `schemas/**`, `docs/openapi.json`
- Any core module (`critics`, `findings`, `disputes`, `policies`, `storage`, `retention`, `auth`,
  `audit`, `systems`, `observability`, `ingestion`)

**Gate commands, in order**

```bash
uv run pytest tests/unit/test_git_ci_client.py
uv run pytest
uv run mypy
uv run ruff check .
uv run ruff format --check .
uv run python -c "import sys; sys.exit(0 if 'subprocess' not in open('src/nyx/adapters/git_ci/github_client.py').read() else 1)"
```

**Live-agent warning.** This is the slice most likely to leak a credential, because it is the only
slice that handles one. Two traps: (a) building the error message from `str(httpx_error)`, which can
carry the request URL; (b) asserting the secret is absent from a string the agent built *using* the
secret, which reports a leak the agent caused (`HANDOFF.md` §7 records this exact mistake). Also:
do not add a `token` field to `GitCiConfig`. If the agent thinks the config needs the value to work,
it has missed that `token_env` names an environment variable and the value is read once at
construction.

---

### Slice 5.2 — normalization

**Status: landed.** `normalize.py` (646 lines) + 20 tests.

**Three corrections to this plan, all found while building or probing:**

1. **`EventInput` has no `scope` field** (`src/nyx/ingestion/inputs.py:66-88`), so §2.4's "set the
   head SHA on the event's `Scope`" was not literally implementable. The event carries it in
   `payload["commit"]`; the evidence carries it on `Scope.commit`, which is what C-001 reads.
2. **The event and the evidence must have distinct `producer_event_id`s.** Sharing one would make
   the evidence's ingestion a spurious `DUPLICATE_EVENT`.
3. **The adapter's evidence could not be stored at all** — see ADR 0008, "Unlinked evidence".
   `EvidenceItem` refused it, and slice 5.2's 20 unit tests did not catch it because they never
   pushed the output through the real ingestion path.

**Deliverables**

- `src/nyx/adapters/git_ci/normalize.py` — the functions in §2.3, plus
  `normalize_commits`, plus a `provider_event_id(kind, repository, external_id)` helper that builds
  the stable `producer_event_id`.

**The normalization rules that matter**

1. **`scope.commit` is always set from the run's head SHA.** This is the field C-001 compares. If a
   run has no SHA, the record is dropped and counted as `skipped`, never emitted with an empty commit.
2. **`event_type` matches `EventType`'s pattern** (`^[a-z0-9]+(\.[a-z0-9][a-z0-9_-]*)+$`). A
   `KeyError` or `ValueError` here is a rejection, not a crash.
3. **`producer_event_id` is stable and namespaced**: `gh:{repository}:check:{id}`. Two different
   runs must never collide; the same run refetched must always collide.
4. **`EvidenceItem.kind` is `ci_test_result`** for check runs and `test_result` for Actions jobs —
   both are in `EvidenceKind` and both are in C-001's `_VERIFICATION_KINDS`
   (`c001_completion_vs_verification.py:39`).
5. **The pass/fail marker must be in the title or description.** C-001 reads it with
   `_SUCCESS_MARKER` / `_FAILURE_MARKER` word-boundary regexes (`c001_completion_vs_verification.py:41-52`).
   `"success"` and `"failure"` as bare words are what the critic needs; a conclusion of
   `"completed"` alone would be invisible to C-001. The adapter must state the conclusion in words.
6. **`access_policy_ref` is one of the four known `evidence:*` read values** (§2.4).
7. **`trust_class` is `observed`.** Never `tool_verified`, never `signed_attestation` — a GitHub API
   response is a record, not an attestation.
8. **`occurred_at` is the run's `completed_at` when present, else `started_at`, else the fetch time.**
   It is never omitted: `Event.occurred_at` is required and NYX does not default it
   (`docs/implementation/HANDOFF.md` §8: "Timestamps on recorded events are required, not
   defaulted"). A missing timestamp is a skipped record, not a `utc_now()` guess.
9. **`limitations` is non-empty** — `EvidenceItem.limitations` has `min_length=1`
   (`src/nyx/domain/evidence.py:69-71`). The adapter's default limitation is the honest one:
   *"GitHub's record of the run; the pipeline may have been influenced by the same actor whose
   claim is under assessment, and the record establishes the run happened, not that the claim is
   true."*

**Tests — `tests/unit/test_git_ci_normalize.py`**

1. Positive: a `success` check run → one `EventInput` with `event_type="ci.check.completed"` and one
   `EvidenceInput` with `kind="ci_test_result"`, `scope.commit` = the head SHA, and `"success"` in
   the title or description.
2. Positive: a `failure` check run → the same shape, with `"failure"` in the text and
   `trust_class="observed"`.
3. Positive: `producer_event_id` is identical for two calls on the same input and different for two
   different runs.
4. Negative: a run with no head SHA → skipped, and the skip is *reported* (returned in a
   `NormalizationReport`), not silently dropped.
5. Negative: a run with no `completed_at` and no `started_at` → skipped (timestamps are required).
6. Negative: an unknown conclusion → the record is emitted with the conclusion text preserved and no
   invented pass/fail word; assert that `"pass"`/`"fail"` do not appear if the source did not say so.
7. Negative: a payload with an unexpected shape (missing keys, wrong types) → a typed
   `NormalizationError` naming the field, never a `KeyError` traceback.
8. Boundary: `normalize` output validates against `EventInput` / `EvidenceInput`
   (`model_validate` succeeds) — the adapter's output must be acceptable to the gateway.

**EXCLUSIVE files this slice may create or modify**

- `src/nyx/adapters/git_ci/normalize.py` (new)
- `tests/unit/test_git_ci_normalize.py` (new)

**FORBIDDEN files**

- Everything listed for slice 5.1, plus `src/nyx/adapters/git_ci/adapter.py` (5.3) and
  `src/nyx/adapters/git_ci/local_git.py` (5.4)
- `src/nyx/ingestion/inputs.py` — the adapter *consumes* these models and must not extend them. If
  a field is genuinely missing, that is an orchestrator decision recorded as an open question, not a
  slice edit.
- `src/nyx/critics/**` — the adapter must not special-case C-001's regexes beyond producing text the
  domain allows; changing the critic to suit the adapter is backwards.

**Gate commands, in order**

```bash
uv run pytest tests/unit/test_git_ci_normalize.py
uv run pytest
uv run mypy
uv run ruff check .
uv run ruff format --check .
```

**Live-agent warning.** The agent will be tempted to make normalization "helpful" by inventing a
commit when GitHub did not report one, or by writing `"passed"` into a description whose conclusion
was `"neutral"`. Both are fabrications of evidence, which is the one thing NYX exists to avoid. The
correct behaviour is to skip the record and report the skip. If the agent finds itself editing
`c001_completion_vs_verification.py` to match its output, stop.

---

### Slice 5.3 — adapter assembly and the live contract test

**Deliverables**

- `src/nyx/adapters/git_ci/adapter.py` — `GitCiAdapter` (§2.3). `fetch_for_ref(ref)` calls the
  client, normalizes, and returns the input list. `normalize(raw)` accepts a raw GitHub payload
  (already-fetched JSON) so the adapter can be driven without a network.
- `tests/contract/test_git_ci_adapter.py` — **the Phase 5 contract test.**

**The contract test's shape.** It stands up a real HTTP server on localhost that speaks just enough
of the GitHub API, points `GitHubCiClient` at it through an injected `httpx` transport, and drives
the adapter end to end. This is the `scripts/probe_telegram_live.py` pattern: a real socket, a real
client, a fake *server*. It is not a unit test with a mock object, and it is not a test that imports
the project's own test helpers.

The test also drives the output through the **real** `IngestionService` against a migrated SQLite
database, so it proves the adapter's output is acceptable to the boundary — not merely that it
validates as a model.

**EXCLUSIVE files this slice may create or modify**

- `src/nyx/adapters/git_ci/adapter.py` (new)
- `tests/contract/test_git_ci_adapter.py` (new)

**FORBIDDEN files**

- All files owned by slices 5.0–5.2 and 5.4–5.5
- `src/nyx/ingestion/**` — the adapter consumes the gateway; it does not modify it
- `tests/conftest.py` — shared fixtures; if a fixture is genuinely needed, ask the orchestrator
- `src/nyx/api/app.py`, `src/nyx/cli/main.py`
- `migrations/**`, `schemas/**`, `docs/openapi.json`

**Gate commands, in order**

```bash
uv run pytest tests/contract/test_git_ci_adapter.py
uv run pytest
uv run mypy
uv run ruff check .
uv run ruff format --check .
uv run alembic upgrade head
```

**Live-agent warning.** The agent will want to import `IngestionService` and call
`service.ingest_event(...)`. That is correct and required. What it must **not** do is import
`IngestionGateway` directly and call `ingest_event` on it without a session — the gateway expects an
open `Session` (`gateway.py:251-258`) and a sessionless call will fail in a way that looks like a
product bug. If the test needs a session, use the `session_factory` fixture from `tests/conftest.py`
with `session_scope`.

---

### Slice 5.4 — local-git provider

**Deliverables**

- `src/nyx/adapters/git_ci/local_git.py` — `LocalGitClient`, the same interface as
  `GitHubCiClient` (`fetch_check_runs`, `fetch_actions_runs` → `list[dict[str, Any]]`), reading a
  **local repository** instead of the GitHub API.
- `tests/unit/test_git_ci_local_git.py`.

**What "local git" means here.** The provider reads the repository's `.git` directory with the
**standard library only** — `.git/HEAD`, `.git/refs/**`, `.git/packed-refs`, and `.git/logs/HEAD` — and
shapes what it finds into the same dict shape `normalize.py` consumes, so **normalization does not
change**. It is the cheapest way to prove the adapter contract is provider-independent, which is the
whole argument for the boundary.

> **No `subprocess` — decided (§8, O6).** The first draft of this plan ran `git` as a subprocess. That
> was rejected: `tests/security/test_verification_allowlist.py` fails the build if `subprocess`,
> `os.system`, `shell=True`, `eval(`, or `exec(` appears anywhere in `src/`, and a reviewed allowlist
> entry for a shell-out is a weaker guarantee than not having the shell-out. Reading `.git` directly
> keeps gate G1 intact and keeps scenario 16's hole closed by construction rather than by review.
>
> **Documented limitation.** `.git/logs/HEAD` (the reflog) carries old sha, new sha, author, email,
> timestamp, and message per line, so the provider can emit `commit.observed` events and ref state.
> It does **not** read git objects, so it cannot read commit contents, file lists, or diffs. An
> operator who needs those has the GitHub provider. This limitation is stated in the module docstring
> and in `docs/adr/0008`, not left for a user to discover.

**Repository path is untrusted configuration.** `local_git.py` validates the path with
`resolve_within` (`src/nyx/ingestion/paths.py:41`) before reading anything — `../../` in a configured
repository path is a path-traversal bug, and a local-git provider is exactly where an operator would
point at a directory they did not mean to expose.

**Tests — `tests/unit/test_git_ci_local_git.py`**

1. Positive: against a real temporary git repository (created by the test), the provider returns the
   commit as a run-shaped dict with the correct SHA, read from `.git/logs/HEAD` and `.git/refs/**`.
2. Positive: `normalize` on that dict produces the same `EventInput`/`EvidenceInput` shape as slice
   5.2 — assert field-by-field equality with the GitHub path's output for an equivalent run.
3. Negative: a repository path that does not exist → `GitCiError` with code `not_found`, no raise.
4. Negative: a repository path containing `..` → refused by the path guard before anything is read.
5. Negative: a directory that exists but is not a git repository (no `.git`) → code `not_found`, no
   raise.
6. Boundary: the same `producer_event_id` is produced for the same commit, so a refetch is a
   duplicate.
7. Boundary: the module source contains no `subprocess`, `os.system`, `shell=True`, `eval(`, or
   `exec(` — read the file and assert it, so the decision in §8/O6 cannot silently regress.

**EXCLUSIVE files this slice may create or modify**

- `src/nyx/adapters/git_ci/local_git.py` (new)
- `tests/unit/test_git_ci_local_git.py` (new)

**FORBIDDEN files**

- Everything owned by slices 5.0–5.3 and 5.5
- `src/nyx/ingestion/paths.py` — consume `resolve_within`, do not modify it
- `src/nyx/adapters/git_ci/normalize.py` — the whole point is that it does not change
- `tests/security/test_verification_allowlist.py` — **not touched.** The no-subprocess decision
  means this slice needs no allowlist entry, and adding one would reopen the hole it exists to close
- `scripts/**` — no script is needed; the provider reads `.git` directly

**Gate commands, in order**

```bash
uv run pytest tests/unit/test_git_ci_local_git.py
uv run pytest tests/security/test_verification_allowlist.py
uv run pytest
uv run mypy
uv run ruff check .
uv run ruff format --check .
```

**Live-agent warning.** The agent will be tempted to reach for `subprocess.run(["git", "log", ...])`
because it is the familiar way to read a repository. **Do not.** It breaks gate G1, and it is the
arbitrary-command-execution hole scenario 16 closes. Read `.git` with `pathlib` and the standard
library. If the agent believes `.git` cannot be read that way, it is wrong — the reflog and refs are
plain text — and it should stop and report rather than "fix" it with a subprocess.

---

### Slice 5.5 — fixture provider and the disablement test (the Phase 5 exit criterion)

**Deliverables**

- `src/nyx/adapters/git_ci/fixture_provider.py` — `FixtureCiClient`, the same interface, reading a
  checked-in JSON fixture. This is the offline path: no network, no git, no token. It makes the
  adapter usable in CI and in the demo.
- `tests/integration/test_adapter_disablement.py` — **the test that is the phase's exit criterion.**

**The disablement contract.** SPEC §19: *"adapters can be disabled independently and core NYX still
works."* Concretely, all of the following must hold, and each is a test:

1. **Default off.** `adapter_enabled` is empty; `GitCiAdapter` is never constructed; `nyx doctor`
   reports `optional_integrations: none enabled`; the full test suite passes with no adapter.
2. **Enabled.** `adapter_enabled={"git-ci": True}` with a valid `git_ci_repository` and a
   `git_ci_token_env` that is set → the adapter is constructed, `health()` returns `OK` or
   `DEGRADED`, and a fetch produces records the gateway accepts.
3. **Disabled while configured.** The adapter is registered in the registry but
   `adapter_enabled` is empty → `nyx doctor` says `registered: git-ci; enabled: false`, and
   **no fetch is attempted**. Assert the fake client recorded zero requests.
4. **Broken config fails closed.** `adapter_enabled={"git-ci": True}` with no `git_ci_token_env`
   → `Settings()` raises `ValueError` naming the variable. The service does not start with a
   half-configured adapter.
5. **Unreachable host degrades, never 500s.** With the adapter enabled and the host unreachable,
   `health()` is `DEGRADED`, and `GET /api/v1/ready` is still `200` (or `503` only for the
   database, never for the adapter). **An optional adapter being down must never make NYX
   unready.** This is the single most important assertion in the phase.
6. **The other adapters are unaffected.** With `git-ci` enabled and `fixture` disabled, the
   fixture path is not constructed. (With only one adapter implemented, this is asserted at the
   registry level: `enabled()` returns only the enabled one.)
7. **Core is untouched.** With the adapter disabled, the full existing suite passes unchanged, and
   a finding can still be raised, rebutted, verified, and disposed through the API — asserted by the
   existing tests running green, not by a new one.
8. **No secret in effective configuration.** `settings.display_effective_config()` contains
   `git_ci_token_env` (a name) and never the token value. Assert with a real-looking token in the
   environment and a search of the rendered JSON.

**EXCLUSIVE files this slice may create or modify**

- `src/nyx/adapters/git_ci/fixture_provider.py` (new)
- `tests/integration/test_adapter_disablement.py` (new)
- `tests/fixtures/git_ci/` (new directory) — one check-runs fixture and one actions-runs fixture,
  modelled on the existing `tests/fixtures/` files

**FORBIDDEN files**

- Everything owned by slices 5.0–5.4
- `src/nyx/config.py` — the fields already exist from slice 5.0. If a field is missing, that is a
  slice 5.0 defect, not a slice 5.5 edit.
- `src/nyx/api/routes/health.py` — readiness must not gain an adapter check. The test asserts
  readiness ignores adapters; the route is not modified to achieve that.
- `tests/integration/test_health_and_doctor.py` — its assertions about optional integrations already
  exist and must keep passing unchanged; do not edit it to accommodate the adapter.

**Gate commands, in order**

```bash
uv run pytest tests/integration/test_adapter_disablement.py
uv run pytest
uv run mypy
uv run ruff check .
uv run ruff format --check .
uv run alembic upgrade head
uv run nyx doctor
```

`uv run nyx doctor` is in this slice's gate set because the disablement contract is a *doctor*
contract: `optional_integrations` must report the adapter's state honestly. Read its output; do not
assume it passed.

**Live-agent warning.** The agent will be tempted to make the adapter "helpfully" fall back to the
fixture provider when GitHub is unreachable, or to make `ready` report the adapter. Both are wrong.
A fallback is an undisclosed second source of evidence, and a readiness check on an optional adapter
turns an integration into a dependency — the exact thing Phase 5's exit criterion forbids. If the
agent finds itself editing `health.py` or adding a fallback path, stop.

---

## 4. Contract test plan — `tests/contract/test_git_ci_adapter.py`

The contract test is the Phase 5 deliverable SPEC §17.1 requires for "each adapter". It asserts the
adapter's *observable behaviour* against the boundary, not its internals.

### 4.1 Positive cases

| # | Assertion |
| --- | --- |
| P1 | `GitCiAdapter` satisfies `isinstance(adapter, Adapter)` and `adapter.adapter_id == "git-ci"`. |
| P2 | `normalize(raw_github_check_runs_payload)` returns at least one `EventInput` and one `EvidenceInput`, both of which pass `model_validate` and are accepted by `IngestionService.ingest_event` / `ingest_evidence` against a migrated database. |
| P3 | The ingested event's `scope.commit` equals the run's head SHA. |
| P4 | The ingested evidence's `kind` is `ci_test_result` and its `scope.commit` equals the same SHA. |
| P5 | The ingested evidence's `access_policy_ref` is one of the four known `evidence:*` read values (§2.4). |
| P6 | `health()` returns `state=OK` when the fake server responds, with `detail` free of any credential. |
| P7 | `fetch_for_ref("main")` produces records whose `producer_event_id` values are stable across two calls. |
| P8 | Refetching the same run through `IngestionService` returns `status="duplicate"` with `DUPLICATE_EVENT` — the adapter's identity key works end to end. |
| P9 | The adapter's records drive C-001: a completion claim for commit `abc123` plus adapter-produced evidence for `def456` produces a finding whose `counterevidence_ids` / `supporting_evidence_ids` reference real evidence ids (no invented ids). |
| P10 | A completion claim plus adapter-produced evidence for the **same** commit produces **no** finding — the negative case that matters as much as P9. |

### 4.2 Negative cases

| # | Assertion |
| --- | --- |
| N1 | `health()` against an unreachable host returns `state=DEGRADED`; it never raises and never hangs past `timeout_seconds`. |
| N2 | `normalize` on a payload with a missing head SHA returns no records and reports the skip. |
| N3 | `normalize` on a payload with no timestamps returns no records and reports the skip. |
| N4 | `normalize` on a payload with an unknown conclusion emits the record with the conclusion preserved and **does not** write `"passed"` or `"failed"` anywhere the source did not say so. |
| N5 | `normalize` on a malformed payload (wrong types, missing keys) raises a typed `NormalizationError` naming the field — never a `KeyError` or `AttributeError` traceback. |
| N6 | The client sends no request at all when `allow_private_targets` is `False` and the host resolves to a private target. |
| N7 | The token appears in **none** of: `GitCiError` messages, `repr(client)`, `AdapterHealth.detail`, the effective configuration, or the structured log output. |
| N8 | An HTTP 401 produces `GitCiError(code="unauthorized")` and the adapter's `health()` reports `DEGRADED` — it does not silently treat an auth failure as "no runs". |
| N9 | A response larger than `max_runs_per_fetch` is truncated and the truncation is reported, not silent. |
| N10 | `fetch_for_ref` with no configured repository raises a typed configuration error, not a `TypeError` on `None`. |
| N11 | `GitCiAdapter` performs no write: assert the fake server recorded **only** `GET` requests (method allowlist), and that no `POST`/`PATCH`/`DELETE` was attempted. |
| N12 | The adapter never composes a finding: after a full fetch and ingest, the `findings` table is empty unless a critic was explicitly run. |

### 4.3 How the test is built

- A real HTTP server on `127.0.0.1` (stdlib `http.server` in a thread, or `pytest-httpserver` only if
  it is already a dependency — it is not, so use stdlib).
- The client is pointed at it through an injected `httpx.BaseTransport`, the same seam as
  `src/nyx/conversational/client.py:53`. **No network.**
- The ingestion assertions use the `session_factory` fixture and a migrated database, as
  `tests/conftest.py` provides.
- The test is written **outside** the project's test helpers for the secret-hygiene assertion (N7):
  the token is placed only in an environment variable, and the assertion searches outputs for it.
  Do not construct the search string from the token (`HANDOFF.md` §7).

---

## 5. Gate set

Every slice ends with the project's own gates run by the **orchestrator**, not by the child that
wrote the code — a module's tests were written by whoever wrote the module.

```bash
uv run pytest                    # full suite
uv run mypy                      # strict, src only
uv run ruff check .
uv run ruff format --check .
uv run alembic upgrade head      # from a CLEAN database
uv run nyx doctor                # Phase 5 adds the adapter to the integrations check
uv run python -c "from nyx.api.openapi import write_openapi; write_openapi()"   # only if a route changed
```

Read `${PIPESTATUS[0]}` or drop the pipe. A red gate is a defect in the slice, not a note.

**Gates that fire on ordinary Phase 5 work:**

- **G1 — `tests/security/test_verification_allowlist.py`** scans `src/` for `subprocess`,
  `os.system`, `shell=True`, `eval(`, `exec(`. Slice 5.4 is the only slice that could trip it (§3,
  slice 5.4). Every other slice must leave `src/` free of them.
- **G2 — `tests/contract/test_openapi_current.py`** compares `docs/openapi.json` to a freshly
  rendered document. **Phase 5 adds no route**, so this should not fire. If it fires, a slice touched
  `api/app.py` or a route module — that is a boundary violation, not a regeneration task.
- **G3 — `tests/integration/test_cli_contract.py`** asserts every registered Typer command offers
  `--json` as a boolean flag. If the orchestrator adds `nyx adapter list|status`, the command is
  discovered automatically and must carry `--json`.
- **G4 — `tests/unit/test_config.py`** will fail if a new `Settings` field has no default or
  violates an existing invariant. Slice 5.0 owns this.
- **G5 — `tests/contract/test_api_cli_consistency.py`** compares API and CLI error families. The new
  `ADAPTER_DISABLED` / `ADAPTER_UNAVAILABLE` codes must appear in `docs/api-error-conventions.md` or
  the consistency test will not know them.
- **G6 — mypy strict.** `Adapter` is a `Protocol`; an adapter class that does not structurally
  satisfy it will not type-check where it is registered. This is the point.
- **G7 — the boundary grep (new, slice 5.0).** A test asserting that no module under
  `src/nyx/adapters/` imports from `nyx.critics`, `nyx.findings`, `nyx.disputes`,
  `nyx.policies`, `nyx.storage`, `nyx.retention`, `nyx.auth`, `nyx.systems`, or `nyx.audit`; and
  that no module outside `src/nyx/adapters/` imports `nyx.adapters` except `api/app.py` and
  `cli/main.py`. This is the executable form of `docs/architecture.md` §3.

**Per-slice proof, beyond the gates:**

| Slice | Proof that it works, not that it is registered |
| --- | --- |
| 5.0 | `AdapterRegistry.states()` reports `DEGRADED` for an adapter whose `health()` raises, and the exception message is absent. |
| 5.1 | Drive the client against a real local HTTP server; read the request path and header. |
| 5.2 | Feed a real GitHub check-runs payload captured from the API docs; assert the normalized scope. |
| 5.3 | Ingest through the real `IngestionService` against a migrated database; read the rows back. |
| 5.4 | Run against a real temporary git repository created by the test. |
| 5.5 | `uv run nyx doctor` with the adapter enabled, disabled, and misconfigured; read all three outputs. |

**Independent verification (the repo's own discipline):** one delegated child per wave, with a
separate context, verifying an already-committed slice against the SPEC text — specifically the
*absence* assertions (no write path, no secret in any output, no subprocess, no import from core),
which a slice's own tests cannot catch in both directions.

---

## 6. Disablement path — Phase 5's exit criterion

**The criterion (SPEC §19):** *adapters can be disabled independently and core NYX still works.*

### 6.1 The three states

| State | What it means | How it is reached | What core NYX does |
| --- | --- | --- | --- |
| **Not registered** | The adapter code exists but no instance is in the registry. | The default. `src/nyx/adapters/git_ci/` is importable but nothing constructs it. | Nothing. Unchanged. |
| **Registered, disabled** | The adapter is constructed and in the registry, but `adapter_enabled` does not name it. | `adapter_enabled={}` (default) or a name mismatch. | Nothing. `nyx doctor` reports `registered: git-ci; enabled: false`. No fetch, no health probe. |
| **Registered, enabled** | The adapter is constructed and fetches. | `adapter_enabled={"git-ci": True}` **and** a valid `git_ci_repository` **and** a set `git_ci_token_env`. | Core NYX is unaffected; the adapter adds records. |

**The rule that makes "disabled" real:** an adapter that is disabled is **not constructed**. A
constructed-but-ignored adapter still has a `health()` method that can be called, and a health
method that fetches can hang. Disabling must remove the object, not skip a call. This is recorded in
`docs/adr/0008` (slice 5.0).

### 6.2 How each adapter is turned off independently

```
NYX_ADAPTER_ENABLED='{"git-ci": false}'      # or simply omit the key
```

- **No code change.** The registry still lists the adapter; `enabled()` filters on the settings.
- **No configuration file edit.** The settings are environment-driven (`Settings`,
  `src/nyx/config.py:37-46`).
- **No restart of anything else.** The adapter is constructed per process; the next process start
  omits it.
- **Failure is silent by design.** A disabled adapter produces no log line, no metric, and no
  finding. The only trace is `nyx doctor`'s `optional_integrations` message.

### 6.3 What must keep working with every adapter off

Asserted by `tests/integration/test_adapter_disablement.py` (slice 5.5):

1. `POST /api/v1/events` ingests and returns 201.
2. `POST /api/v1/claims` registers a scoped claim.
3. `POST /api/v1/evidence` registers evidence.
4. `POST /api/v1/critics/run` runs the deterministic pack and composes a finding.
5. `POST /api/v1/findings/{id}/rebuttals` files a rebuttal; `.../verification-jobs` runs verification;
   `.../dispositions` records a disposition.
6. `GET /api/v1/ready` is `200`.
7. `nyx doctor` exits `0`.
8. The full existing suite passes unchanged.

**An adapter being down is not a NYX outage.** `GET /api/v1/ready` checks the database and migrations
only (`src/nyx/api/routes/health.py:26-51`); it must not gain an adapter check. `nyx doctor` already
reports configured optional integrations as a **pass** with the configured names
(`src/nyx/cli/main.py:464-476`) — Phase 4 fixed exactly this defect (defect #7: "reported any
*configured* optional integration as a failure while never checking availability"). Slice 5.5 must
extend that message to `registered: ...; enabled: ...` **without** changing its pass/fail semantics.

### 6.4 The one-way door

Enabling an adapter must never be required to *disable* it. There is no state to clean up: the
adapter writes records through the ordinary ingestion path, and those records are ordinary domain
records that remain valid (and remain auditable) after the adapter is turned off. Turning the
adapter off stops new observations; it does not retract old ones. That is the correct behaviour and
it is stated here so no future slice "fixes" it by deleting records — which would violate the
append-only rule.

---

## 7. Sibling ecosystem survey — for LATER adapters only

SPEC §14 item 7: *"Ecosystem-specific adapters for ATHENA, ARGUS, NOESIS, DMRX, and Ghost Factory
after their event contracts are inspected."* SPEC §14 also says: *"Do not assume these projects
already expose compatible interfaces. Research each target's current APIs, licence, maintenance, and
data-access constraints before implementing an adapter."*

This survey is **read-only reconnaissance from the local filesystem and public documentation**. It is
not an inspection of any sibling's code in depth, and it does not license an adapter. Every row
separates *observed* from *unknown*.

| System | Where it lives | Interface it exposes (observed) | What is unknown |
| --- | --- | --- | --- |
| **ATHENA** | `C:\Users\PC\The-Agency\athena\gateways\` — a *subdirectory of The-Agency*, not a standalone repo. `gateway_agent.py` is a governance-hook + health-monitoring module with a `HealthStatus` enum (`HEALTHY`/`DEGRADED`/`UNHEALTHY`/`UNKNOWN`) and an async `GovernanceHook = Callable[[dict], Awaitable[dict]]`. The-Agency's README states ATHENA is "a **separate peer platform**, not an Agency component", and that any ATHENA integration would be an explicit optional adapter. | A governance-hook shape and a health enum. **No HTTP surface, no event schema, and no port were observed.** | Whether ATHENA exposes an HTTP API at all; its event/claim schema; its licence; whether `athena/gateways/` is the whole of ATHENA or a client library for it. `athena-global-skills` (`github.com/athena254/Athena-global-skills`) is referenced by The-Agency's README as a separate source of patterns, **not present on this machine**. |
| **ARGUS** | **Not found on this machine.** No directory, no reference in The-Agency's README beyond the ecosystem diagram, and no mention in `Drift/spec.md`. | Nothing observed. | Whether ARGUS is a repository, a service, or a planned system. Its research-claim schema (SPEC §1 lists "research claims, investigation chains, source quality, conclusions, and uncertainty" as the intended observation surface, but that is NYX's *intent*, not ARGUS's interface). **Unknown.** |
| **DANNY** | **Not found on this machine as a project.** The only hit is `github.com/danny-dis/Drift` in Drift's CI badge — an account name, not a system. The-Agency's `docs/SPEC.md` lists "DANNY and ZOEY remain user-facing/personality systems". | Nothing observed. | Whether DANNY is a distinct codebase, a module inside another project, or a planned system. Its intent/preference claim schema. **Unknown.** |
| **NOESIS** | **Not found on this machine.** Referenced as a role in `Drift/spec.md` §1 ("durable cross-system memory/knowledge substrate when integrated") and in The-Agency's `docs/SPEC.md` ecosystem diagram ("NOESIS provides persistent memory"). | Nothing observed. `Drift` itself has a `memory.py`, `capture_store.py`, `compaction.py`, and an `/overview` endpoint returning `ideas`, `projects`, `mind_map`, `events` — but **Drift is not NOESIS**, and treating it as such would be an invented integration. | Whether NOESIS exists as code anywhere; its memory-provenance schema (SPEC §1 lists "memory provenance, temporal conflicts, derived knowledge, source lineage"); whether Drift's memory model is compatible with it. **Unknown.** |
| **DMRX** | `C:\Users\PC\dmr-X` — a TypeScript/Bun monorepo, package `dmr-x` v0.5.21. `apps/gateway/src/server.ts` registers route modules under a `/v1` prefix. | Observed in `server.ts:716-740`: `chatRoutes` → `/v1`, `anthropicRoutes` → `/v1`, `modelsRoutes` → `/v1`, plus `admin`, `bandit`, `mcp-admin`, `a2a-proxy`, `images`, `embeddings`, `rerank`, `moderation`, `audio`, `ocr`, `video`, `3d`. Confirmed paths: `POST /v1/chat/completions` (`chat.routes.ts:1313`), `POST /v1/messages` (`anthropic.routes.ts:540`), `GET /v1/models` (`models.routes.ts:12`), `POST /v1/route` (`route.routes.ts:30`), `GET /v1/admin/a2a/*` (`a2a-proxy.routes.ts:200-311`). `agent.routes.ts` exposes `GET/POST /agents`, `GET /agents/analytics/costs`, `POST /instances/:id/pause|resume|wake|sleep`. | Whether a **usage/cost report** endpoint exists with a stable schema (SPEC §1 wants "request routing, provider/model selection, usage reports, policy decisions, and discrepancies between reported and observed costs"); whether the A2A surface is a stable contract or an admin proxy; the licence (not inspected); whether the gateway is currently running. `docs/implementation/HANDOFF.md` §5 records it as running on `127.0.0.1:3000` as of 21 October 2026. |
| **Ghost Factory** | **Not found as a repository on this machine.** Referenced in `dmr-X/README.md:18` ("ATHENA, ARGUS, Ghost Factory, Claude Code, Codex ... can all use DMR-X. None are dependencies."), in `Drift/spec.md` §1 ("GHOST FACTORY: software engineering/execution"), and in The-Agency's `docs/SPEC.md` §27 ("Ghost Factory builds. THE AGENCY evaluates what Ghost Factory builds."). The-Agency's README also mentions "dark/ghost factory" as a prototype that "do[es] not imply a complete Forge". | Nothing observed. The-Agency's `docs/SPEC.md` §27 describes a *pipeline* (build → security test → pass/fail/findings → remediation → verification), not an API. | Whether Ghost Factory is a repository, a planned system, or a role inside another project; its event schema for "repository changes, branch/PR state, build and test evidence, review, integration, deployment, and maintenance claims" (SPEC §1); its licence. **Unknown.** The Phase 5 Git/CI adapter observes **GitHub**, not Ghost Factory — it produces the evidence a Ghost Factory adapter would later correlate, and it does not assume Ghost Factory exists. |

### 7.1 What this survey licenses

- **Nothing.** No sibling adapter is proposed in this plan.
- The Git/CI adapter is the *substrate* a later Ghost Factory adapter would sit on: it produces
  `ci_test_result` and `commit` evidence with a `scope.commit`, which is exactly the shape a
  Ghost-Factory-facing adapter would correlate against branch/PR state.
- Any later sibling adapter must repeat §7 as a fresh reconnaissance (repositories change) and must
  record its own SPEC §12 dependency review. SPEC §12's final line: *"A project can be suitable for
  research but unsuitable for production."*

### 7.2 What must not be done

- Do not assume a sibling exposes a compatible interface.
- Do not vendor a sibling's code, in either direction.
- Do not write a sibling adapter against a schema invented from SPEC §1's *intent*. SPEC §1 describes
  what NYX wants to observe; it is not a contract any sibling has agreed to.

---

## 8. Open questions

These were questions. Each is now decided, and the decision is recorded here so it is not
re-litigated silently by a later slice.

- **O1 — `src/nyx/adapters/` has no `__init__.py`.** **Decided: yes.** It is the package root; slice
  5.0 creates `src/nyx/adapters/__init__.py`. The directory is empty and untracked.
- **O2 — step 15 names `docs/adr/0007-git-ci-adapter.md`.** **Decided: renumber.** ADR 0007 is
  `0007-auth-modes.md` (Phase 4); this plan uses `0008-adapter-boundary-and-disablement.md`. The
  checklist row is corrected (§10, D2).
- **O3 — step 15 names `src/nyx/adapters/git_ci/*`.** **Decided: keep** that directory.
- **O4 — the `Adapter` protocol is a sketch, not code.** **Decided: acceptable.** `health()` takes no
  arguments; `normalize()` returns producer *input* models; `AdapterRegistry.states()` is added. The
  sketch's `AdapterDescriptor` is kept. The deviation and its reason are in §2.2.
- **O5 — OpenTelemetry.** **Decided: out of Phase 5.** SPEC §14 item 3 (OTLP ingestion) and item 4
  (evaluation integrations) are separate adapters and need their own dependency review. The Git/CI
  adapter is the phase. Recorded as a candidate for a later phase, not as a deferral nobody owns.
- **O6 — the local-git provider needs `subprocess`, which `src/` forbids.** **Decided: no subprocess.**
  Slice 5.4 reads the repository with the standard library only — `.git/HEAD`, `.git/refs/**`,
  `.git/packed-refs`, and `.git/logs/HEAD` (whose lines carry old sha, new sha, author, timestamp, and
  message). That is enough for `commit.observed` events and ref state, and it keeps gate G1 intact.
  **Documented limitation:** the local-git provider reads refs and the reflog, not git objects, so it
  cannot read commit contents or diffs. An operator who needs that has the GitHub provider.
- **O7 — `git_ci_token_env` names an environment variable.** **Decided: yes.** It matches the existing
  `auth_principals` pattern and lets one deployment hold several adapters' tokens.
- **O8 — `max_runs_per_fetch` truncates silently by default.** **Decided: truncation is reported**, in
  a `NormalizationReport` on the returned result. A bounded, reported truncation; never an unbounded
  fetch.
- **O9 — scope.** **Decided: six slices (5.0–5.5), one adapter (Git/CI) with three providers**
  (GitHub, local-git, fixture). SPEC §14's remaining adapter categories are later phases.
- **O10 — the working tree is not clean.** **Decided: resolved.** The in-flight scenario-24 change was
  committed as `b6b2858` before this plan was accepted; `git show HEAD:src/nyx/findings/lifecycle.py`
  contains the gate (7 occurrences). Slice 5.0 starts from a clean tree.

---

## 9. Explicit non-goals for Phase 5

- Any sibling adapter (ATHENA, ARGUS, DANNY, NOESIS, DMRX, Ghost Factory). §7 is reconnaissance.
- Any write path to any external system. ADR 0004.
- Any change to the deterministic core: no new critic, no lifecycle change, no schema change.
- Any new third-party dependency (§1.3).
- Any change to `FindingLifecycle`, `Capability`, or the append-only tables.
- Any new route. The adapter is reachable through the existing ingestion surface and the CLI.
- Any new acceptance scenario. Phase 5 has none; it is gated by the adapter contract tests and the
  disablement test.

---

## 10. Countercheck findings — claims in the existing docs that are wrong or stale

Each was found by reading the file or running the command, not by inference.

| # | Claim | Where | What is actually true |
| --- | --- | --- | --- |
| **D1** | "Phase 0 … **no independent security or maturity audit has been performed**." | `roadmap.md` §Phase 0, open box; `current-state.md` §1.2 | **Still true as written, and now closed by §1 of this plan.** The versions and licences are recorded; the audit is the table in §1.1 plus the rejections in §1.2. |
| **D2** | Step 15 expects `docs/adr/0007-git-ci-adapter.md`. | `task-checklist.md:26` | **False.** `0007-auth-modes.md` already exists (Phase 4). The next free number is 0008. |
| **D3** | `src/nyx/adapters/` is listed as the Phase 5 home. | `task-checklist.md:26-27`, `architecture.md` §2 | **True but empty.** `git ls-files src/nyx/adapters` returns nothing; there is no `__init__.py`. The directory exists on disk only. |
| **D4** | Step 6 expects `src/nyx/evidence/*`. | `task-checklist.md:17` | **The checklist row is stale, but the directory is not missing.** `ls src/nyx/evidence/` shows an **empty directory** (no entries, untracked — git does not track empty directories, and `git log --all -- src/nyx/evidence` is empty). The path in the checklist names modules that were never written there; the evidence registry lives in `src/nyx/domain/evidence.py`, `src/nyx/ingestion/gateway.py`, and `src/nyx/storage/repositories/evidence.py`. Corrected to name those. |
| **D5** | "`certifi 2026.10.18` carried 2 known CVEs, found by running the dependency scan locally." | `roadmap.md` §Phase 4, defect #9 | **Fixed.** The venv has `certifi 2026.10.19` and `uv audit` reports **no known vulnerabilities in 70 packages**. The defect row is history, not current status. |
| **D6** | The working tree is clean at HEAD `a76c8da`. | `git status` | **Resolved.** The in-flight scenario-24 change is now committed as **`b6b2858`**, pushed, and the tree is clean apart from this plan. It is recorded here because the plan was written while that change was uncommitted, and slice 5.0's boundary grep scans `lifecycle.py`. |
| **D7** | "Scenario 24 … is **not** enforced as a gate on `confirmed`." | `acceptance-tests.md` | **Resolved.** The docs and the code now agree at HEAD: `git show HEAD:src/nyx/findings/lifecycle.py \| grep -c self_consistency_gate` → **7**, and `acceptance-tests.md` marks scenario 24 `[x]` at HEAD. |
| **D8** | "`nyx doctor` does not probe an LLM endpoint's reachability. It reports what is configured." | `HANDOFF.md` §"Honest limitations" | **True.** `cli/main.py:464-476` reports configured integrations as a **pass**. Slice 5.5 must extend that message to `registered: …; enabled: …` without changing its pass/fail semantics. |
| **D9** | SPEC §14 item 1: "HTTP/CLI/file adapter for reliable end-to-end local use." | `SPEC.md:1615` | **Partially delivered, never labelled.** The HTTP and CLI paths exist (`api/routes/events.py`, `cli/main.py` `ingest event|batch`). **There is no file adapter**: `grep -rn 'file adapter\|file-based fixture'` finds only SPEC mentions, no code. `nyx ingest event <file>` reads a file into the *CLI*, which is not an adapter. **This is an open item, not a Phase 5 deliverable in this plan** — flagging it so it is not silently assumed done. |
| **D10** | The conversational layer's `httpx` usage is the only HTTP client in NYX. | `grep -rn 'import httpx' src/` | **True.** `conversational/client.py`, `conversational/llm.py`, `conversational/telegram/adapter.py`. The Git/CI adapter becomes the **second** `httpx` caller and the first outside `conversational/`. This is why §1.1 records `httpx` as a runtime dependency rather than an extra. |

---

## 11. Delivery checklist for the phase

- [ ] SPEC §12 review table complete and the "no new dependency" verdict recorded (§1).
- [ ] `Adapter` protocol, registry, health, and config fields landed (slice 5.0).
- [ ] ADR 0008 records the boundary and the disablement decision.
- [ ] GitHub CI client bounded, secret-safe, read-only (slice 5.1).
- [ ] Normalization produces gateway-acceptable inputs with a real `scope.commit` (slice 5.2).
- [ ] `tests/contract/test_git_ci_adapter.py` passes P1–P10 and N1–N12 (slice 5.3).
- [ ] Local-git provider passes the same normalization contract (slice 5.4).
- [ ] `tests/integration/test_adapter_disablement.py` proves all eight disablement assertions (slice 5.5).
- [ ] `uv run nyx doctor` reports `registered: git-ci; enabled: false|true` honestly.
- [ ] `docs/openapi.json` unchanged (no route added); `tests/contract/test_openapi_current.py` green.
- [ ] `src/` contains no `subprocess` outside a reviewed allowlist.
- [ ] No module under `src/nyx/adapters/` imports from core; no core module imports `nyx.adapters`.
- [ ] No sibling code vendored, in either direction.
- [ ] The sibling survey (§7) is re-run before any sibling adapter is proposed.
