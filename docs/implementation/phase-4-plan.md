# Phase 4 plan — Security and operational hardening

**Source:** `docs/SPEC.md` §12, §15, §16, §17.2, §18, §19 (Phase 4); `docs/privacy-and-retention.md`;
`docs/threat-model.md`; ADR 0003 and ADR 0006.
**Owner of this plan:** the orchestrating agent. **Status:** proposed, not yet approved.
**Acceptance gate:** SPEC §17.2 scenarios 13–20 pass, each against the test file named in
`docs/implementation/acceptance-tests.md`.

**Last verified against the code:** 21 October 2026, branch `master`, head `5c5d7f6`, working tree
clean, `origin` = `github.com/athena254/NYX`.

---

## 1. What already exists (verified, do not rebuild)

These are the contracts and seams Phase 4 plugs into. Each was read, not assumed.

| Asset | Where | State |
| --- | --- | --- |
| Error codes for Phase 4 | `src/nyx/error_codes.py` | `UNAUTHENTICATED` (401), `FORBIDDEN`, `CAPABILITY_REQUIRED`, `EVIDENCE_ACCESS_DENIED`, `RATE_LIMITED` (429), `DEPENDENCY_UNAVAILABLE` all defined and mapped |
| Auth config | `src/nyx/config.py:58-59,92-98` | `auth_mode: none\|local\|api_key`, `auth_api_key: SecretStr`; non-loopback + `none` already refused |
| Capability seam | `src/nyx/findings/lifecycle.py:86-135,297-309` | `Capability` enum, `CapabilityChecker` Protocol, `DenyAllCapabilityChecker`, `_CAPABILITY_REQUIREMENTS` mapping each guarded target to a capability, `validate_transition` checks **before** the graph |
| Secret-safe config | `src/nyx/config.py:118-147` | `safe_effective_config`, `_looks_secret`, `redact_database_url` |
| Structured logging | `src/nyx/observability/logging.py` | `JsonFormatter` redacts extras through the same `redact()` used for payloads |
| Error envelope | `src/nyx/api/errors.py` | handlers for `ApiError`, validation, HTTP, `SQLAlchemyError`, and unhandled; no traceback escapes |
| Payload/batch limits | `src/nyx/ingestion/limits.py` | `enforce_payload_size`, `enforce_batch_size` |
| Redaction before persistence | `src/nyx/ingestion/redaction.py` | fails closed with `REDACTION_FAILED` |
| Audit hash chain | `src/nyx/audit/writer.py:60-127` | `AuditWriter.append` links `prior_entry_hash` → `entry_hash` per entry |
| Verification allowlist | `src/nyx/disputes/verification.py:104-156` | a check that is not registered cannot run; `VerificationContext` deliberately carries no payload |
| Health/readiness | `src/nyx/api/routes/health.py` | `GET /health`, `GET /ready` |
| `nyx doctor` | `src/nyx/cli/main.py:376-534` | config/db/migration/writable-path checks, secret-safe |
| Evidence classification | `src/nyx/domain/evidence.py:42-80` | `sensitivity: Sensitivity` (`public\|internal\|confidential\|restricted`), `access_policy_ref`, `retention_class` |
| Retention classes | `src/nyx/domain/enums.py:59-64` | `ephemeral`, `standard`, `extended`, `legal_hold`, `derived_metadata` |

## 2. What is missing (the gaps, each with its evidence)

| # | Gap | Evidence |
| --- | --- | --- |
| G1 | **No authentication is enforced.** `auth_mode`/`auth_api_key` are read only by `config.py` and one `doctor` line. Nothing checks a presented credential. | `grep -rn 'auth_mode\|auth_api_key'` → only `config.py`, `cli/main.py:420`, `tests/unit/test_config.py` |
| G2 | **The capability checker is always deny-all.** `app.state.capability_checker = DenyAllCapabilityChecker()`; `deps.get_capability_checker` falls back to deny-all. | `api/app.py:78`, `api/deps.py:69-77` |
| G3 | **Evidence reads are not capability-gated.** `GET /api/v1/evidence/{id}` returns the full `EvidenceItem` with no check. | `api/routes/evidence.py:37-38` |
| G4 | **No audit-chain verifier exists.** The chain is written but never checked; `audit_verify_on_startup` is dead configuration. | `grep -rn 'audit_verify'` → `config.py:88` only; `audit/` holds `writer.py` and `__init__.py` only |
| G5 | **No API rate limiting.** `RATE_LIMITED` exists as a code but no limiter calls it (only the Telegram bot has one, a different mechanism). | `grep -rn 'RATE_LIMITED' src/` → `error_codes.py`, `telegram/` |
| G6 | **No retention or redaction job.** `docs/privacy-and-retention.md` §4–§5 specifies classes and a deletion workflow; no code implements either. | no `src/nyx/retention/`; no purge command in `cli/main.py` |
| G7 | **No `tests/security/` package at all**, and five of the six named Phase 4 test files are absent. | `ls tests/security` → empty; absent: `test_evidence_access.py`, `test_prompt_injection.py`, `test_verification_allowlist.py`, `test_audit_integrity.py`, `test_health_and_doctor.py`, `test_llm_critic_failure.py` |
| G8 | **No API/CLI consistency test** for scenario 21. | `tests/contract/` holds `test_openapi_current.py`, `test_schema_agreement.py` only |
| G9 | **No CI of any kind.** `.github/` is empty — no workflows, no dependency scan, no container scan. | `find .github -type f` → nothing |
| G10 | **No Dockerfile**, so "container scanning in CI" (SPEC §12) has nothing to scan. | no `Dockerfile`/`docker-compose` in the repo root |
| G11 | **No SSRF or path-traversal guard.** `source_ref`/`locator` on evidence are free strings. | `domain/evidence.py:52-55` |
| G12 | **No prompt-injection guard module.** ADR 0006 requires retrieved content to be quoted as data; the conversational layer does this by prompt wording only, and no test asserts it structurally. | `grep` finds no `injection` module; `docs/adr/0006-untrusted-content-boundary.md` is policy only |
| G13 | **No LLM-critic failure handling** for scenario 19's stub path. | no `src/nyx/critics/llm*.py` |
| G14 | **The acceptance harness carries stale SKIPs** for scenarios 15, 16, 17, 19, 20–24. | `scripts/acceptance_harness.py:454-461` |

## 3. Reconciliation — where the tracked artifacts drift from the code

Recorded here rather than silently corrected, per the checkpoint discipline.

1. `docs/implementation/roadmap.md` §Phase 4 lists **"API/CLI consistency tests"** as a work item but
   assigns it no acceptance scenario. `acceptance-tests.md` assigns scenario 21 to it. The roadmap
   summary table should list scenario 21 under Phase 4. **Proposal:** add it; do not edit silently.
2. `acceptance-tests.md` line 47 checks scenario 14 against `tests/unit/test_redaction.py` and
   `tests/integration/test_redaction_persistence.py`. Both exist. Phase 4 **re-validates** 14, so a
   Phase 4 test must additionally cover the *model-processing* clause (currently met only by the
   absence of model calls). See slice 4.4.
3. `roadmap.md` §Phase 1 says "No authentication/authorisation or rate limits (Phase 4)" — accurate.
4. `HANDOFF.md` §"Next move" names Phase 4 as the next block — accurate, and this plan follows it.
5. `scripts/acceptance_harness.py` SKIP reasons are **stale for 6–13** (Phase 3 shipped those) and
   will be stale for 15–20 as this phase lands. Replace each SKIP with a real check as its scenario
   lands; never leave a SKIP in place after the feature exists.
6. `SPEC.md` §18 suggests `tests/e2e/`; nothing requires it for Phase 4. Not built here.

---

## 4. Slice decomposition, file ownership, and the parallel order

Eight slices. **File ownership is exclusive** — two slices never edit the same file. Where a slice
would need a shared file, the orchestrator owns that edit and lands it between waves.

| Slice | Scenario | Files (exclusive) | Wave |
| --- | --- | --- | --- |
| **0. Shared infra** (orchestrator) | — | `src/nyx/config.py`, `.env.example`, `AGENTS.md`, `docs/implementation/phase-4-plan.md`, `src/nyx/auth/__init__.py`, `src/nyx/retention/__init__.py`, `src/nyx/error_codes.py` | 0 |
| **4.1 Auth + capability enforcement** | 13 | `src/nyx/auth/{principal,store,checker,dependency,apikey}.py`, `src/nyx/api/deps.py`, `src/nyx/api/routes/evidence.py`, `src/nyx/findings/lifecycle.py`, `tests/security/test_evidence_access.py` | 2 |
| **4.2 Audit verification** | 17 | `src/nyx/audit/{chain,verifier}.py`, `src/nyx/audit/writer.py`, `src/nyx/cli/main.py`, `tests/integration/test_audit_integrity.py` | 1 |
| **4.3 Rate limiting** | 12 (ops) | `src/nyx/api/rate_limit.py`, `tests/unit/test_rate_limit.py`, `tests/integration/test_rate_limit_api.py` | 2 |
| **4.4 Injection, SSRF, traversal, arbitrary command** | 15, 16 | `src/nyx/ingestion/{injection,url_fetch,paths}.py`, `src/nyx/ingestion/inputs.py`, `tests/security/test_prompt_injection.py`, `tests/security/test_verification_allowlist.py`, `tests/security/test_ssrf_and_paths.py` | 1 |
| **4.5 Retention and redaction workflow** | 12 (ops) | `src/nyx/retention/{policy,service,redaction,tombstone}.py`, `migrations/versions/<rev>_retention_redaction_markers.py`, `tests/unit/test_retention_policy.py`, `tests/integration/test_retention.py` | 2 |
| **4.6 Health/doctor + API/CLI consistency** | 20, 21 | `tests/integration/test_health_and_doctor.py`, `tests/contract/test_api_cli_consistency.py` | 3 |
| **4.7 Failure-safe LLM critic (stub path)** | 19 | `src/nyx/critics/llm.py`, `tests/unit/test_llm_critic_failure.py` | 1 |
| **4.8 CI + container** | 12 (ops) | `.github/workflows/{ci,security}.yml`, `Dockerfile`, `.dockerignore` | 3 |
| **W. Orchestrator wiring** | — | `src/nyx/api/app.py`, `src/nyx/api/middleware.py`, `src/nyx/cli/main.py` (retention + audit commands land in wave 1's slice), `scripts/acceptance_harness.py` | 2→4 |

**Concurrency limit: 2 coding agents at once.** This machine has 2 cores / 4 threads and measured
~725 MB free RAM; each OpenCode process holds 300–900 MB. Wave 1 runs 4.2 + 4.4 in parallel with
4.7 queued; wave 2 runs 4.1 + 4.3 with 4.5 queued. Exceeding this produces silent stalls, not speed.

**Orchestrator-owned edits, landed before any child starts:** `config.py` fields, `.env.example`,
the two package `__init__.py` files, and any new `ErrorCode` members. A child that starts before
those land can create a file the orchestrator then cannot write.

**`src/nyx/api/app.py` and `src/nyx/cli/main.py` are orchestrator-only.** Children deliver modules
and tests; the orchestrator wires middleware, the capability checker, and the new CLI commands. This
is the single rule that keeps the waves collision-free.

---

## 5. Slice specifications (prescriptive)

### Slice 0 — shared infrastructure (orchestrator)

Add to `src/nyx/config.py` `Settings`:

```python
# Authentication and authorisation (Phase 4)
auth_principals: list[dict[str, Any]] = Field(default_factory=list)
# Each entry: {"principal_id": str, "api_key_env": str, "capabilities": list[str]}.
# The raw key is read from the named environment variable, never stored in config.
# `auth_api_key` (pre-existing) stays the single-operator-key case; either one satisfies
# auth_mode='api_key'. Capability names are validated against the Capability enum by
# CapabilityStore.load_from_settings, not here, so config.py does not import the lifecycle.

# API rate limiting (Phase 4)
rate_limit_enabled: bool = True
rate_limit_requests: int = Field(default=120, ge=1)
rate_limit_window_seconds: float = Field(default=60.0, gt=0)
rate_limit_exempt_paths: list[str] = Field(default_factory=lambda: ["/api/v1/health", "/api/v1/ready"])

# Evidence URL and locator policy (Phase 4)
evidence_allow_private_urls: bool = False
evidence_allow_absolute_locators: bool = False

# Retention (Phase 4). Purge is destructive, so it is opt-in.
retention_purge_enabled: bool = False
retention_ephemeral_days: int = Field(default=7, ge=1)
retention_standard_days: int = Field(default=90, ge=1)
retention_extended_days: int = Field(default=365, ge=1)
retention_derived_metadata_days: int = Field(default=730, ge=1)
```

Validators (fail closed, naming the variable) — as landed in `config.py`:

- `auth_mode == "api_key"` with **neither** `auth_api_key` nor `auth_principals` → `ValueError`
  naming both variables.
- Each `auth_principals` entry must carry a non-empty `principal_id`, an `api_key_env`, and at
  least one capability; a missing key names the entry and the variable.
- `rate_limit_requests < 1` is impossible via `Field(ge=1)`.

Mirror every new variable in `.env.example` with a comment stating the safe default and the failure
mode. Add one line to `AGENTS.md` pointing at this plan.

### Slice 4.1 — authentication and capability enforcement (scenario 13)

New package `src/nyx/auth/`:

```python
# principal.py
class AuthenticatedPrincipal(NyxModel):
    id: PrincipalRef
    capabilities: frozenset[Capability]
    auth_mode: AuthMode

# apikey.py
def hash_api_key(key: str) -> str: ...          # sha256 hex; never store the raw key
def keys_match(presented: str, expected_hash: str) -> bool: ...   # hmac.compare_digest

# store.py
@dataclass(frozen=True)
class PrincipalRecord:
    principal_id: str
    key_hash: str
    capabilities: frozenset[Capability]
    description: str

class CapabilityStore:
    def __init__(self, principals: Sequence[PrincipalRecord]) -> None: ...
    def authenticate(self, presented_key: str) -> AuthenticatedPrincipal | None: ...
    def anonymous(self) -> AuthenticatedPrincipal: ...
    def load_from_settings(self, settings: Settings) -> CapabilityStore: ...   # classmethod

# checker.py
class ConfigBackedCapabilityChecker:
    def __init__(self, store: CapabilityStore) -> None: ...
    def has_capability(self, principal: PrincipalRef, capability: Capability) -> bool: ...

# dependency.py
def current_principal(request: Request) -> AuthenticatedPrincipal: ...
def require_capability(capability: Capability) -> Callable[..., AuthenticatedPrincipal]: ...
```

**The trust decision, stated explicitly (needs an ADR — slice 4.1 writes `docs/adr/0007-auth-modes.md`):**

- `auth_mode="none"` is reachable **only** on a loopback bind (already enforced in `config.py`).
  Loopback requests are therefore the local operator and are granted the deployment-owner
  capability set. This preserves every existing local caller, test, and probe — `scripts/probe_live_api.py`
  and the whole test suite call the API with no credential and must keep working.
- `auth_mode="api_key"` requires every request to present a key in `Authorization: Bearer <key>` or
  `X-API-Key`. An unknown or absent key is `401 UNAUTHENTICATED`. A known key without the required
  capability is `403 FORBIDDEN` / `403 EVIDENCE_ACCESS_DENIED`.
- **The CLI is not covered by HTTP auth** and must not be claimed to be: it opens the database
  directly (`nyx-development` skill). State this in the ADR as a documented boundary.

**Evidence gate** — `src/nyx/api/routes/evidence.py`:

- Add `Capability.EVIDENCE_READ` and `Capability.EVIDENCE_RESTRICTED` to the enum in
  `src/nyx/findings/lifecycle.py`.
- `GET /api/v1/evidence/{evidence_id}` requires `EVIDENCE_READ`; when the item's `sensitivity` is
  `restricted` it additionally requires `EVIDENCE_RESTRICTED`.
- `access_policy_ref` is a free string today. Map it to a capability by convention: a value of
  `evidence:restricted` requires `EVIDENCE_RESTRICTED`; anything else requires `EVIDENCE_READ`.
  Reject an unknown `access_policy_ref` at registration time rather than silently downgrading it.
- `GET /api/v1/findings` and `GET /api/v1/findings/{id}` must **omit or tombstone** the payload of a
  restricted evidence item they reference when the caller lacks `EVIDENCE_RESTRICTED`
  (`docs/privacy-and-retention.md` §6). The finding's evidence *ids* stay; the content does not.

**Test — `tests/security/test_evidence_access.py`** (scenario 13):

1. Positive: a principal with `EVIDENCE_RESTRICTED` reads a restricted item and gets its content.
2. Negative: a principal **without** it gets `403 EVIDENCE_ACCESS_DENIED` and the response body
   contains no evidence content.
3. Negative: no credential at all in `api_key` mode → `401 UNAUTHENTICATED`.
4. Boundary: a `confidential` item is readable with `EVIDENCE_READ` alone.
5. Boundary: a finding referencing restricted evidence omits that payload for a caller lacking the
   capability, while its id and classification remain.
6. Boundary: `auth_mode="none"` on loopback still works (regression guard for every existing caller).

### Slice 4.2 — audit-chain verification (scenario 17)

The writer's `_entry_hash` moves to a shared module so the writer and the verifier cannot drift —
one bad shared assumption hiding the same bug everywhere is the failure this repo has already been
bitten by twice.

```python
# src/nyx/audit/chain.py
def entry_hash(*, schema_version, sequence, actor, action, target, timestamp,
               request_id, correlation_id, outcome, reason, prior_entry_hash) -> str: ...

# src/nyx/audit/verifier.py
@dataclass(frozen=True)
class Divergence:
    sequence: int
    entry_id: str
    reason: str     # "prior_hash_mismatch" | "recomputed_hash_mismatch" | "sequence_gap" | "not_genesis"

@dataclass(frozen=True)
class VerificationReport:
    ok: bool
    entries_checked: int
    first_divergence: Divergence | None

class AuditVerifier:
    def __init__(self, repository: AuditRepository) -> None: ...
    def verify(self) -> VerificationReport: ...
```

- `AuditWriter` imports `entry_hash` from `chain.py`. **Its behaviour must not change.**
- **Countercheck finding — corrects an earlier draft of this plan.** There is **no existing test of
  the audit writer's chain logic.** `grep -rn 'AuditWriter' tests/` returns nothing; the two files
  that mention `prior_entry_hash` assert an audit entry's *shape*, not the hash chain. An earlier
  version of this plan claimed "the existing `tests/integration/test_audit_*` coverage is the guard"
  — that was false, and acting on it would have let a refactor of the hash function ship unguarded.
  **Slice 4.2 therefore writes a characterization test first**: `tests/integration/test_audit_chain.py`
  asserts the writer's hash for a known entry set, before `chain.py` is extracted. The extraction is
  then proven by that test staying green, and `test_audit_integrity.py` adds the mutation cases.
- `AuditRepository.list` already orders by `sequence`; the verifier walks it and recomputes.
- `nyx audit verify` — new `audit` Typer group in `src/nyx/cli/main.py`, `--json`, exit `0` on a
  clean chain and `1` on divergence, never a traceback.
- `audit_verify_on_startup` becomes real: on startup, verify and **log a warning**; do not refuse to
  boot. A diverged chain is a finding, not a boot failure. `nyx doctor` reports it as a failed check.

**Test — `tests/integration/test_audit_integrity.py`** (scenario 17): clean chain verifies;
mutating one row's `reason` is detected; deleting a middle row is detected; overwriting an
`entry_hash` is detected; an empty store verifies trivially. Plus a test that states the documented
non-claim: rewriting the whole database and recomputing every hash is **out of scope** (SPEC §5.8,
ADR 0003) — assert the report's wording rather than pretending otherwise.

### Slice 4.3 — API rate limiting

```python
# src/nyx/api/rate_limit.py
@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: float

class SlidingWindowRateLimiter:
    def __init__(self, *, limit: int, window_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None: ...
    def check(self, key: str) -> RateLimitDecision: ...
    def reset(self) -> None: ...

class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, limiter: SlidingWindowRateLimiter,
                 exempt_paths: Sequence[str] = (), key_for: Callable[[Request], str]) -> None: ...
    async def dispatch(self, request, call_next) -> Response: ...
```

- Key by authenticated principal id when present, else client host. Never by a header the caller
  can set.
- `/health` and `/ready` are **exempt** — a limiter that blocks health checks is how a dead
  deployment reports itself alive.
- `429 RATE_LIMITED` with a `Retry-After` header, through the existing error envelope.
- **Documented non-claim:** in-memory, per-process. A multi-process deployment needs a shared store;
  record it in `docs/operations.md` rather than implying cluster-wide limiting.

**Tests:** `tests/unit/test_rate_limit.py` (window math, burst at the boundary, exhaustion, reset,
clock injection); `tests/integration/test_rate_limit_api.py` (429 + `Retry-After`, health exempt,
per-principal isolation).

### Slice 4.4 — injection, SSRF, traversal, arbitrary command (scenarios 15, 16)

```python
# src/nyx/ingestion/injection.py
UNTRUSTED_CONTENT_NOTICE: str
def quote_as_untrusted(text: str) -> str: ...              # fenced + marked; fence cannot be closed from inside
def build_model_prompt(*, question: str, evidence: Sequence[EvidenceItem]) -> str: ...

# src/nyx/ingestion/url_fetch.py
class UrlFetchDenied(Exception): ...
def is_private_target(host: str) -> bool: ...              # resolves DNS; RFC1918, loopback, link-local, CGNAT 100.64/10, IPv6 ULA/::1
def validate_evidence_url(url: str, *, allow_private: bool = False) -> None: ...

# src/nyx/ingestion/paths.py
class PathTraversalError(Exception): ...
def resolve_within(base: Path, candidate: str) -> Path: ...
```

**Wiring — the guard must have a real caller or it is a placeholder (AGENTS.md rule 3):**

- `validate_evidence_url` is called from the evidence input validation in `src/nyx/ingestion/inputs.py`
  when `source_ref` is an `http(s)` URL. With `evidence_allow_private_urls=false` (the safe default)
  a URL resolving to a private target is rejected at registration with a structured error. This is
  the SSRF boundary at the ingestion edge and it is enforced, not decorative.
- `resolve_within` is called on evidence `locator` when it is path-like, rejecting `..` escapes and
  absolute paths outside the data dir unless `evidence_allow_absolute_locators=true`.
- `build_model_prompt` is the single function the conversational layer must use to compose a prompt
  with retrieved content; it carries the ADR 0006 notice and fences the content as data.

**Tests:**

- `tests/security/test_prompt_injection.py` (scenario 15): a payload containing an instruction
  cannot close the fence or escape the quoted block; policy resolution reads **only** from the
  policy store, so an evidence payload carrying a forged policy instruction changes no resolved rule
  (asserted against `policies/engine.py`); tool permissions come from trusted configuration, never
  from evidence text.
- `tests/security/test_verification_allowlist.py` (scenario 16): an event naming an unregistered
  check is refused **before** anything executes; `VerificationContext` structurally carries no
  payload field (assert on `dataclasses.fields`); a registered check that raises produces a `failed`
  job and leaves the finding unchanged; and a **regression guard that greps `src/` for
  `subprocess`, `os.system`, `shell=True`, `eval(`, `exec(`** and fails the build if any appears
  outside an allowlisted allowlist. This is the real control for "no arbitrary shell execution".
- `tests/security/test_ssrf_and_paths.py`: private-target URLs refused by default and allowed when
  configured; DNS-rebinding shape (a name that resolves to a private address) refused; `..` and
  absolute-path escapes refused.
- Scenario 14 re-validation: a test that the model prompt path receives **redacted** content only —
  redact, then build, then assert the secret appears nowhere in the prompt.

### Slice 4.5 — retention and redaction workflow (SPEC §11.3)

```python
# src/nyx/retention/policy.py
@dataclass(frozen=True)
class RetentionPolicy:
    ephemeral_days: int
    standard_days: int
    extended_days: int
    derived_metadata_days: int
    def is_expired(self, record: HasRetentionClass, now: datetime) -> bool: ...   # legal_hold is never expired

# src/nyx/retention/tombstone.py
def tombstone_payload(original: str, *, action: str, actor: str, reason: str) -> str: ...

# src/nyx/retention/redaction.py
@dataclass(frozen=True)
class RedactionOutcome:
    record_id: str
    action: str                 # "payload_redacted" | "hash_removed"
    fields_affected: tuple[str, ...]
    audit_entry_id: str

class RedactionService:
    def __init__(self, session: Session, *, actor: PrincipalRef, allowlist: CapabilityStore) -> None: ...
    def redact(self, *, target_type: str, target_id: str, action: str, reason: str) -> RedactionOutcome: ...

# src/nyx/retention/service.py
@dataclass(frozen=True)
class PurgeReport:
    scanned: int
    purged: int
    skipped_legal_hold: int
    dry_run: bool

class RetentionService:
    def __init__(self, session: Session, *, policy: RetentionPolicy, actor: PrincipalRef) -> None: ...
    def purge_due(self, *, now: datetime, dry_run: bool) -> PurgeReport: ...
```

**Migration — `migrations/versions/<rev>_retention_redaction_markers.py`:** add `redacted_at`,
`redacted_by`, `redaction_reason`, and `redaction_action` to **`events`, `claims`, `evidence_items`
only**. Do **not** add them to findings, rebuttals, dispositions, or policy versions: those are
append-only and are never rewritten (ADR 0003). A disposition that relied on redacted evidence
records that the evidence was redacted and remains contestable (`privacy-and-retention.md` §5).

- `legal_hold` records are never purged; the report counts them as `skipped_legal_hold`.
- Every action appends an audit entry (actor, target, action, reason, extent) — the fact of a
  change is recorded even when the content is gone.
- `nyx retention purge --dry-run|--apply` and `nyx retention redact ...` — **orchestrator** owns
  these CLI commands (wave 3), because `cli/main.py` is orchestrator-only. Slice 4.5 delivers the
  modules and tests.
- `retention_purge_enabled=false` by default: purge is destructive and must be opted into.

**Tests:** `tests/unit/test_retention_policy.py` (per-class expiry, legal_hold never expires, boundary
at the exact duration); `tests/integration/test_retention.py` (a due record is tombstoned with an
audit entry; a legal-hold record survives; `--dry-run` changes nothing; findings/rebuttals/
dispositions rows are byte-identical before and after a purge — asserted directly, not by absence).

### Slice 4.6 — health, doctor, and API/CLI consistency (scenarios 20, 21)

- `tests/integration/test_health_and_doctor.py` (scenario 20): health and readiness report
  dependency state honestly; a stopped database makes readiness report not-ready **without** a 500;
  `nyx doctor` output contains no secret — construct settings with a real-looking key, run doctor,
  assert the key and its hash appear nowhere in stdout; doctor exits non-zero on a failed check.
- `tests/contract/test_api_cli_consistency.py` (scenario 21): for a representative error from each
  family (validation, auth, capability, rate limit, storage), the API's `code` and the CLI's
  `code` agree; every CLI failure exits non-zero with no traceback (the existing
  `test_cli_contract.py` is the model to extend, not to replace).
- Extend `nyx doctor` with three new checks — audit chain verified, rate limiter configured,
  retention policy loaded — each secret-safe. **Orchestrator** owns this (wave 3).

### Slice 4.7 — failure-safe LLM critic, stub path (scenario 19)

```python
# src/nyx/critics/llm.py
class LlmTransportError(Exception): ...
class LlmTimeoutError(LlmTransportError): ...
class LlmInvalidOutputError(LlmTransportError): ...

@runtime_checkable
class LlmTransport(Protocol):
    def complete(self, *, prompt: str, schema: dict[str, Any], timeout_seconds: float) -> str: ...

class LlmCritic:      # implements the same Critic interface as the deterministic critics
    def __init__(self, *, transport: LlmTransport, schema: ..., clock: Callable[[], datetime] = utc_now) -> None: ...
    def run(self, context) -> CriticOutcome: ...    # never composes a finding on failure
```

- Strict structured-output validation against a JSON Schema; unknown evidence IDs are rejected;
  a timeout, invalid JSON, a refusal, or a schema violation produces an `ERROR`/`INCONCLUSIVE`
  outcome and **no finding is composed and no state changes**.
- The default transport with no provider configured returns `ERROR` and composes nothing. It is
  fail-closed, not a stub that pretends to work.
- **Boundary with Phase 6:** this slice owns the interface, the validation, and the failure
  handling. Phase 6 owns the real provider transport. Do not build the provider adapter here.

**Test — `tests/unit/test_llm_critic_failure.py`** (scenario 19): timeout → no finding, no state
change; invalid JSON → same; schema violation → same; unknown evidence id → same; refusal → same;
and the positive case (valid structured output) composes a finding with the evidence ids it
referenced. Assert on the database, not on the return value.

### Slice 4.8 — CI, dependency scanning, container scanning

- `.github/workflows/ci.yml`: `uv sync`, `uv run pytest`, `uv run mypy`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run alembic upgrade head` against a fresh SQLite file.
- `.github/workflows/security.yml`: dependency scanning (`uv audit` or `pip-audit`) on a schedule
  and on push; container scanning (Trivy) **only if a Dockerfile is added** — see open question Q2.
- `Dockerfile` + `.dockerignore` if Q2 is answered "add".

---

## 6. Gate set, per slice

Every slice ends with the project's own gates run by the orchestrator, not by the child that wrote
the code — a module's tests were written by whoever wrote the module.

```bash
uv run pytest                    # full suite
uv run mypy                      # strict, src only
uv run ruff check .
uv run ruff format --check .
uv run alembic upgrade head      # from a CLEAN database
uv run python -c "from nyx.api.openapi import write_openapi; write_openapi()"   # if any route changed
```

Read `${PIPESTATUS[0]}` or drop the pipe. A red gate is a defect in the slice, not a note.

Two gates that fire on ordinary Phase 4 work, and will fail the build if a slice forgets them:

- **`tests/contract/test_openapi_current.py`** compares `docs/openapi.json` to a freshly rendered
  document. Slice 4.1 adds a security dependency to the evidence route, which changes the rendered
  OpenAPI. Any slice that touches a route must regenerate it.
- **`tests/integration/test_cli_contract.py`** introspects the registered Typer command tree and
  asserts **every** command offers `--json` as a boolean flag defaulting to `False`. `nyx audit verify`
  and `nyx retention purge` are discovered automatically; if either lacks `--json`, the build fails.
  This is the desired behaviour — it is the machine interface contract.

**Per-slice proof, beyond the gates:**

| Slice | Proof that it works, not that it is registered |
| --- | --- |
| 4.1 | Drive `GET /api/v1/evidence/{id}` against a real app with `auth_mode="api_key"` and two principals; read the status codes. |
| 4.2 | Run `nyx audit verify` against a migrated database, then mutate a row by hand and run it again. |
| 4.3 | Exhaust the limit against a live app; read the 429 and the `Retry-After` header. |
| 4.4 | The `src/` grep guard runs in CI; the prompt test feeds a payload that tries to close the fence. |
| 4.5 | Purge against a migrated database with a due record and a legal-hold record; re-read both. |
| 4.6 | Run doctor with a secret in the config and grep its stdout for it. |
| 4.7 | Assert on the database after each failure mode, not on the return value. |

**Independent verification (the repo's own discipline):** one Hermes `delegate_task` child per
wave, with a separate context, verifying an already-committed slice against the SPEC text —
specifically the absence assertions (no write path for a model, no shell execution, no restricted
content in a response), which the slice's own tests cannot catch in both directions.

---

## 7. Open questions for the owner

These are decisions I will not make silently.

- **Q1 — Retention purge is destructive.** `retention_purge_enabled=false` by default, tombstones
  rather than deletes, legal_hold never purged, every action audited. Confirm.
- **Q2 — Container scanning needs a container.** There is no Dockerfile. Either add a minimal
  `Dockerfile` + `.dockerignore` so Trivy has something real to scan, or record container scanning
  as "applies when a container is published" and ship dependency scanning only.
- **Q3 — `access_policy_ref` is a free string.** I propose mapping `evidence:restricted` →
  `EVIDENCE_RESTRICTED` and everything else → `EVIDENCE_READ`, rejecting unknown values at
  registration. If the deployment already uses `access_policy_ref` values with meaning, say so.
- **Q4 — Scope check.** Phase 4 as specified here is eight slices. Confirm this is the whole phase,
  or name what to cut.

## 8. Explicit non-goals for Phase 4

- Model critics and external adapters (Phase 5, 6).
- Real provider transport for the LLM critic (Phase 6).
- Multi-process / shared-store rate limiting.
- A dashboard or UI.
- Any weakening of `FORBIDDEN_TOOLS` or the append-only rules.
