# NYX architecture

**Status:** Phase 0 design baseline. No module in this document is implemented yet.
**Contract:** `docs/SPEC.md` Part II. If this document and the SPEC disagree, the SPEC wins.

## 1. Deployment shape

Phase 1 ships **one deployable Python service**, not microservices. The FastAPI application, Typer CLI,
critic engine, evidence registry, persistence layer, and adapters all run in the same process and use
the same SQLite database. There is no message broker, no second database, and no network hop between
logical modules.

The module boundaries in this document are code boundaries, not deployment boundaries. They exist so
that a module can be tested in isolation and, if it ever becomes necessary, extracted later without
rewriting the domain model. Extracting a module into a separate service is explicitly out of scope for
Phases 1–6.

## 2. Module map

All paths are relative to `src/nyx/`.

### `nyx/` (package root)

**Responsibility:** package identity, version, and the smallest possible set of shared exports.
**Must not:** contain business logic or import optional adapters.
**Interface sketch:** `__version__: str`.

### `domain/`

**Responsibility:** framework-free domain types and invariants. Defines the canonical shapes for
monitored systems, events, claims, evidence, critics, findings, rebuttals, dispositions, and audit
entries. Owns ID conventions, UTC timestamp rules, canonical JSON, digest helpers, and the finding
lifecycle state machine.
**Must not:** open a database session, call HTTP, read configuration, redact payloads, decide a
finding's truth, or persist anything. It must not depend on FastAPI, Typer, SQLAlchemy, or any adapter.
**Public interface sketch:**

```python
class MonitoredSystem: ...
class Event: ...
class Claim: ...
class EvidenceItem: ...
class CriticRun: ...
class Finding: ...
class Rebuttal: ...
class Disposition: ...
class AuditEntry: ...

def new_id(prefix: str) -> str: ...
def utc_now() -> datetime: ...
def canonical_json(value: object) -> bytes: ...
def sha256_digest(value: bytes) -> str: ...  # returns "sha256:<hex>"
class FindingLifecycle:
    def assert_transition(self, current: FindingStatus, target: FindingStatus) -> None: ...
```

System registration is a domain capability, not a separate top-level package: the
`MonitoredSystem` model lives here, persistence lives in `storage`, and registration endpoints live in
`api`/`cli`.

### `ingestion/`

**Responsibility:** the only write path for incoming systems, events, claims, and evidence. Applies
schema validation, size and batch limits, sensitivity classification, redaction **before persistence**,
canonicalisation, content hashing, idempotency, and deduplication. Calls `storage` through a unit of
work and writes an audit entry for accepted mutations.
**Must not:** decide whether behaviour is acceptable, create findings, treat a producer's assertion as
verified, run critics, call a model with unredacted content, or perform external actions.
**Public interface sketch:**

```python
class IngestionService:
    def register_system(self, manifest: SystemManifest) -> IngestResult: ...
    def ingest_event(self, event: EventInput, idempotency_key: str | None) -> IngestResult: ...
    def ingest_event_batch(self, events: list[EventInput]) -> list[IngestResult]: ...
    def register_claim(self, claim: ClaimInput) -> IngestResult: ...
    def register_evidence(self, evidence: EvidenceInput) -> IngestResult: ...

@dataclass(frozen=True)
class IngestResult:
    status: Literal["created", "duplicate", "rejected"]
    record_id: str | None
    error: StructuredError | None
```

### `evidence/`

**Responsibility:** the evidence registry. Stores and retrieves evidence metadata and references,
verifies stored content against its recorded digest, evaluates freshness and scope coverage, records
source lineage and independence, and enforces evidence access policy.
**Must not:** claim that a hash, signature, or attestation proves the truth of a statement; silently
modify or delete evidence; copy large or restricted payloads into findings; treat repeated summaries
as independent corroboration.
**Public interface sketch:**

```python
class EvidenceRegistry:
    def register(self, item: EvidenceItem) -> EvidenceItem: ...
    def get(self, evidence_id: str, principal: Principal) -> EvidenceItem: ...
    def verify_integrity(self, evidence_id: str) -> IntegrityResult: ...
    def check_freshness(self, evidence_id: str, scope: Scope) -> FreshnessResult: ...
    def lineage(self, evidence_id: str) -> list[LineageLink]: ...
    def assert_readable(self, evidence_id: str, principal: Principal) -> None: ...
```

### `critics/`

**Responsibility:** the common critic interface and registry, plus the deterministic baseline critics
C-001 through C-010. Each run records critic type, critic/rule/model version, input references and
hashes, start and completion times, result, rationale, and failure details. This module also hosts the
self-consistency critic C-010 and the routing layer that chooses checks by claim type and risk.
**Must not:** write findings directly to storage, change dispositions, execute arbitrary shell commands
from event content, treat model output as deterministic verification, or make an external action.
Invoking a verification tool requires an allowlisted adapter with resource limits.
**Public interface sketch:**

```python
class Critic(Protocol):
    critic_id: str
    critic_version: str
    critic_type: Literal["deterministic", "tool", "llm"]
    def run(self, context: CriticContext) -> CriticResult: ...

class CriticRegistry:
    def register(self, critic: Critic) -> None: ...
    def list(self) -> list[CriticDescriptor]: ...
    def get(self, critic_id: str) -> Critic: ...

class CriticScheduler:
    def run(self, critic_ids: list[str], input_refs: list[str]) -> list[CriticResult]: ...
```

C-001 through C-010 are defined in SPEC §8. The critic result contract is SPEC §7.2: proposition,
scope, observations, supporting and counter evidence IDs, limitations, rationale, recommended next
step, and `requires_human_review`.

### `findings/`

**Responsibility:** finding composition, severity, deduplication, lifecycle enforcement, querying, and
rendering. The composer validates that every referenced evidence ID exists and is readable, separates
observations, inferences, recommendations, and unknowns, and refuses a `confirmed` state without the
evidence and rationale required by SPEC §3.2.
**Must not:** invent evidence IDs, verify its own claims, accept risk, bypass lifecycle rules, delete a
prior finding version, or perform remediation.
**Public interface sketch:**

```python
class FindingComposer:
    def compose(self, result: CriticResult, context: FindingContext) -> Finding: ...

class FindingService:
    def get(self, finding_id: str, principal: Principal) -> Finding: ...
    def query(self, filters: FindingFilters, cursor: str | None) -> Page[Finding]: ...
    def transition(
        self, finding_id: str, target: FindingStatus, actor: Principal, reason: str
    ) -> Finding: ...
    def find_duplicates(self, candidate: Finding) -> list[DuplicateLink]: ...
```

### `disputes/`

**Responsibility:** rebuttals, counterevidence, verification requests, disposition changes, and
history. Every rebuttal and disposition appends a new record; a new disposition supersedes but never
deletes its predecessor. Enforces capability checks and transactional state changes.
**Must not:** mutate or erase prior findings, rebuttals, evidence, or dispositions; let a monitored
system decide its own disposition; close a finding merely because the target says it is fixed; perform
the remediation.
**Public interface sketch:**

```python
class DisputeService:
    def submit_rebuttal(self, finding_id: str, rebuttal: RebuttalInput) -> Rebuttal: ...
    def request_verification(self, finding_id: str, request: VerificationRequest) -> JobRef: ...
    def record_disposition(
        self, finding_id: str, disposition: DispositionInput, actor: Principal
    ) -> Disposition: ...
    def history(self, finding_id: str, principal: Principal) -> list[HistoryEntry]: ...
```

### `policies/`

**Responsibility:** versioned policy and rule configuration, a minimal built-in deterministic rule
interface, and an optional OPA adapter boundary. Every evaluation records policy ID, policy version,
input references, and result. Policy changes record the new version, author, time, and reason.
**Must not:** allow evidence or monitored content to change policy; self-approve a policy change;
execute the action a policy recommends; depend on OPA for the MVP to function.
**Public interface sketch:**

```python
class PolicyCatalog:
    def get(self, policy_id: str, version: str | None = None) -> PolicyDocument: ...
    def list_versions(self, policy_id: str) -> list[PolicyDescriptor]: ...
    def publish(self, document: PolicyDocument, actor: Principal) -> PolicyVersion: ...

class PolicyEvaluator(Protocol):
    def evaluate(self, policy_id: str, version: str, inputs: Mapping[str, object]) -> PolicyDecision: ...
```

### `audit/`

**Responsibility:** append-only audit history for security-relevant and lifecycle events. Records actor,
action, target type and ID, timestamp, request/correlation ID, outcome, reason, and prior-entry hash or
reference. Provides audit queries and within-model chain verification.
**Must not:** update or delete entries, store secrets or restricted payloads, or describe a local hash
chain as tamper-proof against an administrator who can rewrite the database.
**Public interface sketch:**

```python
class AuditLog:
    def append(self, entry: AuditEntryInput) -> AuditEntry: ...
    def query(self, filters: AuditFilters, cursor: str | None) -> Page[AuditEntry]: ...
    def verify(self) -> AuditVerificationResult: ...
```

### `storage/`

**Responsibility:** database engine and session management, unit-of-work/transaction boundaries,
repositories, migrations, retention jobs, and query pagination. Enforces append-only behaviour with
application checks and database permissions where practical.
**Must not:** contain business rules, perform redaction, call critics, call external systems, or expose
ORM objects outside the repository boundary. It never stores secrets that ingestion failed to redact.
**Public interface sketch:**

```python
class Database:
    def session(self) -> Iterator[Session]: ...
    def upgrade(self) -> None: ...

class UnitOfWork:
    systems: SystemRepository
    events: EventRepository
    claims: ClaimRepository
    evidence: EvidenceRepository
    findings: FindingRepository
    disputes: DisputeRepository
    policies: PolicyRepository
    audit: AuditRepository
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
```

### `api/`

**Responsibility:** versioned HTTP API under `/api/v1`, request/response validation, authentication,
capability authorisation, bounded payloads, cursor pagination, request IDs, structured errors, health
and readiness. It calls application services; it does not implement them.
**Must not:** bypass ingestion or dispute services, return restricted evidence based only on finding
access, include secrets in errors, permit unbounded batches, or bind remotely without explicit
configuration.
**Public interface sketch:** `create_app() -> FastAPI` plus routers for systems, events, claims,
evidence, critics, findings, rebuttals, verification, dispositions, history, health, and readiness.
The endpoint list is SPEC §9.1.

### `cli/`

**Responsibility:** Typer application (`nyx`) with human-readable and JSON output, non-zero exit codes
on failure, and a `doctor` command that checks configuration, database, migrations, and writable paths
without printing secrets. Calls the same services as the API.
**Must not:** duplicate business logic, write directly to the database, print credentials, or claim a
health check passed without performing it.
**Public interface sketch:** `app: typer.Typer` with `system`, `ingest`, `claim`, `evidence`, `critic`,
`finding`, `audit`, and `doctor` command groups (SPEC §10).

### `adapters/`

**Responsibility:** optional integrations that translate external data into NYX domain inputs. Planned
order: Git/CI, OpenTelemetry/OpenInference, policy, provenance, and ecosystem-specific adapters after
their interfaces are inspected. Each adapter declares permissions, failure modes, and a contract test.
**Must not:** become a prerequisite for core operation, execute arbitrary commands, fetch arbitrary
URLs (SSRF), follow symlinks or paths outside a configured root, or grant tool permissions based on
content. Core modules must never import an optional adapter.
**Public interface sketch:**

```python
class Adapter(Protocol):
    adapter_id: str
    version: str
    def health(self) -> AdapterHealth: ...
    def normalize(self, raw: object) -> list[EventInput | ClaimInput | EvidenceInput]: ...

class AdapterRegistry:
    def register(self, adapter: Adapter) -> None: ...
    def enabled(self) -> list[AdapterDescriptor]: ...
```

### `observability/`

**Responsibility:** structured JSON logging, metrics, and tracing configuration with redaction.
Provides stable event names and bounded metric labels.
**Must not:** log raw payloads, restricted evidence, secrets, user IDs, full claim text, or unbounded
labels; tracing must not capture restricted evidence by default.
**Public interface sketch:**

```python
def configure_observability(settings: ObservabilitySettings) -> None: ...
def get_logger(name: str) -> logging.Logger: ...
class Metrics:
    def increment(self, name: str, labels: Mapping[str, str] | None = None) -> None: ...
    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None: ...
```

## 3. Dependency direction

```text
api / cli / adapters
        |
        v
ingestion / evidence / critics / findings / disputes / policies
        |
        v
      domain
        |
        v
      storage

observability and audit are cross-cutting; services call them explicitly.
```

Rules:

- `domain` imports nothing from the other `nyx` modules except itself.
- `storage` imports `domain`; it does not import services, API, CLI, or adapters.
- Services import `domain` and repository interfaces; they do not import `api`, `cli`, or adapters.
- `api` and `cli` may import application services; neither may import the other.
- `adapters` may import `domain` and ingestion input types; core modules never import `adapters`.
- Optional dependencies (OPA, OpenTelemetry, external evaluators) sit behind protocols and are imported
  lazily so the no-LLM and no-adapter paths work.

## 4. Separation of concerns

Adapted from SPEC §4 to the real package names.

| SPEC role | NYX module(s) | May do | Must not do |
| --- | --- | --- | --- |
| Collector | `ingestion`, `adapters` | Gather, validate, redact, canonicalise, deduplicate, persist observations | Decide whether behaviour was acceptable or create findings |
| Evidence registry | `evidence`, `storage` | Preserve records, identity, hashes, source pointers, lineage, freshness, and ACLs | Silently change evidence or equate integrity with truth |
| Detectors | `critics` | Identify possible issues and emit scoped hypotheses with method and version | Declare a verdict or perform remediation |
| Critic | `critics`, `findings` | Explain a contradiction and propose a falsifiable challenge with evidence references | Invent evidence, execute untrusted commands, or certify its own output |
| Verifier | `critics`, `disputes`, `policies`, `adapters` | Run deterministic or independently corroborating checks and record environment and tool versions | Trust a monitoring tool's raw event content, or run arbitrary commands |
| Disposition authority | `disputes`, `policies` | Resolve according to policy, risk, and capability; append a new disposition | Delete prior history, let the target decide its own case, or accept risk without authority |
| Enforcement mechanism | Outside NYX | Act only through a separate authorised mechanism | Be implemented as automatic NYX action in the MVP |
| Self-audit | `critics` (C-010), `audit`, `observability` | Audit NYX's evidence choices, transitions, critic versions, and history completeness | Treat self-audit as proof that NYX is correct |

## 5. Initial policy catalogue (Phase 0 draft)

This is the initial catalogue required by SPEC §10 Phase 0. Policy documents, versions, and evaluation
engines are implemented in Phase 2; the identifiers and rule intent below are the contract for that
work. All policies are evaluated against persisted records, not free text alone.

| Policy ID | Rule intent | Expected failure behaviour |
| --- | --- | --- |
| `engineering.required-checks` | A completion or deployment claim must link a passing required check for the same artifact, commit/build, environment, and scope. | Emit a scoped verification gap or contradiction; never assert the code is defective from missing evidence alone. |
| `engineering.approval-required` | A high-impact change or action must have a recorded approval from a principal holding the required capability. | Report the missing approval; do not perform or block the action. |
| `evidence.provenance-required` | Material evidence must record source identity, source reference, locator where applicable, capture time, and scope. | Report the missing fields as a provenance gap, without implying misconduct. |
| `evidence.freshness` | Evidence must be valid for the claim's time window and artifact version. | Mark the evidence stale; do not let it silently verify a newer artifact. |
| `authorization.high-impact-action` | A recorded or proposed high-impact action must have a matching permission or approval record. | Create a finding and recommend escalation; never execute or reverse the action. |
| `dispute.disposition-authority` | Only a principal with the disposition capability may confirm, reject, partially confirm, or accept risk, and must supply a rationale. | Reject the transition with a typed authorization error and leave state unchanged. |
| `retention.redaction-required` | Sensitive payloads must be redacted before persistence and before any model call; restricted evidence requires an explicit capability. | Reject the write or deny the read; never persist the unredacted value. |
| `audit.append-only` | Rebuttals, dispositions, and material lifecycle transitions append new records and never overwrite prior records. | Reject the mutation and preserve the existing history. |
| `nyx.self-consistency` | Every confirmed finding has a rationale, at least one relevant evidence item (or an explicit policy-only exemption with policy and triggering record), valid evidence references, recorded critic versions, and a valid transition history. | Block confirmation or flag the finding as inconsistent. |
| `critic.no-model-as-proof` | Model-generated assessment is labelled as assessment and never as deterministic verification. | Reject or downgrade the result; require independent evidence for high-impact findings. |

## 6. Request flow (planned Phase 1 slice)

1. API or CLI receives a system manifest, event, claim, or evidence reference.
2. `ingestion` validates size, schema version, and required fields; rejects invalid input without a
   partial write.
3. Redaction and sensitivity classification run before persistence.
4. Canonicalisation produces a content hash; idempotency keys and producer event IDs are checked.
5. `storage` writes the record and an audit entry in one transaction.
6. The record is returned with its stable ID and source links.
7. Critic execution is a separate, explicit request. It loads persisted records, runs allowlisted
   checks, and returns critic results.
8. `findings` composes a finding only from existing, readable evidence IDs.
9. `disputes` appends rebuttals, verification jobs, and dispositions; `audit` records each transition.

No step in this flow performs an external action.
