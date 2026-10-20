# Phase 3 plan — Disputes and verification

**Source:** `docs/SPEC.md` §5.7, §6, §9, §10 and §19 Phase 3. Scenarios 10, 11, 22, 23.
**Status:** planned. Phase 2 is complete and committed (`44a0351`, checkpoint `464c929`).
**Prompt for the implementing agent:** `.agent-prompts/phase3.md`.

## What Phase 3 is

The Phase 2 lifecycle machine has the states (`rebuttal_pending`, `verification_pending`,
`rejected`, `inconclusive`, …) but nothing can *reach* them: no rebuttal can be filed, no
verification can run, and no disposition can be recorded. Phase 3 builds the append-only dispute
workflow on top of that machine — and closes the gap that left acceptance scenario 24 unchecked.

Exit criteria (SPEC §19): every finding can be challenged, resolved, withdrawn, or marked
inconclusive **without deleting its history**.

## Acceptance scenarios and their owners

| # | Scenario | Owning test | Phase 3 deliverable that satisfies it |
| --- | --- | --- | --- |
| 10 | A rebuttal appends to history and does not overwrite the original finding. | `tests/integration/test_disputes.py` | Rebuttal model, table, repository, service; history read API. |
| 11 | Counterevidence can lead to rejection or an inconclusive disposition. | `tests/integration/test_disputes.py` | Counterevidence links on the rebuttal; lifecycle rules requiring counterevidence for `rejected` / `inconclusive` after a challenge. |
| 22 | A policy change records the new version and who changed it. | `tests/integration/test_policy_versioning.py` | `policy_versions` table + `PolicyVersionStore`; actor and timestamp recorded on every registration. |
| 23 | A fixed finding remains open until verification or a documented authorised waiver. | `tests/integration/test_finding_lifecycle.py` | **New file.** The rule exists and is unit-tested, but the scenario's owning integration test does not exist yet. |

Scenario 23 is the clearest example of why this plan exists: the behaviour is implemented and unit
tested in `tests/unit/test_finding_lifecycle.py::test_fixed_pending_verification_is_not_closed`, but
the acceptance table names an integration test file that has never been written. Phase 3 must write
it rather than move the box.

## Scaffolding that already exists (do not rebuild)

Verified by inspection at the start of planning:

- `src/nyx/domain/enums.py` — `DispositionOutcome` (7 values), `RebuttalRequestedOutcome` (6 values),
  and the full `FindingStatus` set.
- `src/nyx/domain/ids.py` — `new_rebuttal_id()` (`rbt_`), `new_disposition_id()` (`dsp_`).
- `schemas/rebuttal.schema.json`, `schemas/disposition.schema.json` — Phase 0 contracts with exact
  required fields, enum values, and length bounds. Each states a Pydantic model and an agreement test
  are required.
- `src/nyx/disputes/` — empty package, already in the layout.
- `src/nyx/findings/lifecycle.py` — the validated graph, `_RULES`, `_EDGE_RULES`, the capability
  seam (`Capability.DISPOSITION` / `ACCEPT_RISK` / `ADMINISTER`), and an audit entry per accepted
  transition.
- `src/nyx/policies/engine.py` — `PolicyRegistry` with versioned `Rule`s and a content `digest()`.

## Gaps to close

1. No `Rebuttal`, `Disposition`, or verification-job models, tables, repositories, or migration.
2. `PolicyRegistry` is in-memory only: it versions rules but records no *change* — no actor, no
   timestamp, no reason. Scenario 22 is entirely unbuilt.
3. `TransitionRequest` carries `verification_result` and `waiver` but no link to the rebuttal,
   verification job, policy version, or counterevidence that justified the move.
4. No dispute service, no history surface, no dispute API routes or CLI commands.
5. `rejected` and `inconclusive` require only a rationale. Scenario 11 needs counterevidence to be
   *the* reason a challenge succeeds, not an optional field.
6. Scenario 24: C-010 is registered and tested but is not consulted when a finding is confirmed.

## Work items, in dependency order

### 3.1 Rebuttal and disposition models — `src/nyx/disputes/models.py`

- `Rebuttal` with exactly the SPEC §5.7 / `schemas/rebuttal.schema.json` fields: `id`,
  `schema_version`, `finding_id`, `submitted_by`, `submitted_at`, `argument`, `evidence_ids`,
  `requested_outcome`.
- `Disposition` with `id`, `schema_version`, `finding_id`, `outcome`, `rationale`, `decided_by`,
  `decided_at`, `verification_run_ids`, `policy_version`, `supersedes_disposition_id`.
- Both must use the shared `NyxModel` base, UTC-only timestamps, and prefixed IDs.
- Extend `tests/contract/test_schema_agreement.py` to cover both. Until it passes, the Phase 0
  schemas remain contracts rather than verified artifacts.

### 3.2 Verification job model — `src/nyx/disputes/models.py`

- `VerificationJob`: `id`, `finding_id`, `check_id`, `requested_by`, `requested_at`, `status`
  (`queued` / `running` / `succeeded` / `failed` / `refused`), `result`, `evidence_ids`,
  `tool_run_refs`, `failure_details`, `started_at`, `completed_at`.

### 3.3 Storage — `src/nyx/storage/tables.py`, `migrations/versions/`

- `RebuttalRow` (`rebuttals`), `DispositionRow` (`dispositions`), `VerificationJobRow`
  (`verification_jobs`), `PolicyVersionRow` (`policy_versions`).
- One Alembic migration. Append-only: **no update or delete path** on any of the four tables.
- Repositories in `src/nyx/storage/repositories/`: `RebuttalRepository`, `DispositionRepository`,
  `VerificationJobRepository`, `PolicyVersionRepository`.
- Supersession is a `supersedes_disposition_id` column, never a rewrite. The current disposition is
  derived as the latest by `decided_at`.

### 3.4 Policy versioning — `src/nyx/policies/versions.py` (scenario 22)

- `PolicyVersionStore.register(rule, changed_by, change_reason) -> PolicyVersion` records `rule_id`,
  `version`, `changed_by`, `changed_at`, `digest`, and `change_reason`.
- A version may be superseded by a later registration of the same `rule_id`; the earlier row is
  retained. Reading the *current* version of a rule resolves to the latest.
- `Disposition.policy_version` records which policy version decided it, so a later policy change can
  never retroactively alter a recorded decision.

### 3.5 Verification allowlist — `src/nyx/disputes/verification.py`

- `VerificationChecker` protocol: `check_id`, `run(context) -> VerificationOutcome`.
- A hard allowlist of check IDs. An unknown or unregistered check is **refused**, never executed —
  SPEC §17.2 scenario 16 requires that a verification tool cannot execute an arbitrary command
  supplied by an untrusted event, and Phase 3 must not create the hole that Phase 4 later has to
  close.
- The runner records the environment and exact tool version with every result (SPEC §7 step 8).

### 3.6 Dispute service — `src/nyx/disputes/service.py`

- `submit_rebuttal(...) -> Rebuttal` — appends the rebuttal, records its counterevidence against the
  finding, and drives the finding to `rebuttal_pending` through the lifecycle machine. The original
  finding is never mutated beyond its status pointer (scenario 10).
- `request_verification(...) -> VerificationJob` — enqueues a job for an allowlisted check.
- `run_pending_verification(...) -> VerificationJob` — executes queued jobs through the allowlist.
- `record_disposition(...) -> Disposition` — capability check, mandatory rationale, supersession link,
  policy version, and the corresponding lifecycle transition.
- `get_history(finding_id) -> DisputeHistory` — the finding, every rebuttal, every verification job,
  and every disposition in order. Nothing is ever removed from it.

### 3.7 Lifecycle wiring — `src/nyx/findings/lifecycle.py`

- Extend `TransitionRequest` with `rebuttal_id`, `verification_job_ids`, `policy_version`, and
  `counterevidence_ids`.
- Add a rule: after a challenge, `rejected` and `inconclusive` require counterevidence (scenario 11).
  The rule must state in its error which premise failed.
- Extend `_audit_reason` to record the rebuttal id, verification job ids, and policy version so the
  audit trail is self-explaining.
- **Optional, closes scenario 24:** consult C-010 when a finding is confirmed and refuse if it fires.
  This is a Phase 2 gap surfaced by the Phase 2b checkpoint; it is small and belongs here because it
  needs the storage-backed wiring Phase 3 already builds.

### 3.8 HTTP API — `src/nyx/api/routes/disputes.py`

- `POST /api/v1/findings/{finding_id}/rebuttals`, `GET` on the same path.
- `POST /api/v1/findings/{finding_id}/verification-jobs`,
  `GET /api/v1/verification-jobs/{job_id}`.
- `POST /api/v1/findings/{finding_id}/dispositions`,
  `GET /api/v1/findings/{finding_id}/history`.
- `GET /api/v1/policies/versions`.
- Structured errors per `docs/api-error-conventions.md`; capability failures return
  `CAPABILITY_REQUIRED`, never a silent success. Regenerate and drift-test `docs/openapi.json`.

### 3.9 CLI — `src/nyx/cli/main.py`

```
nyx finding rebut    <finding_id> <file>
nyx finding verify   <finding_id> <check-id>
nyx finding dispose  <finding_id> <outcome>
nyx finding history  <finding_id>
```

`--json` everywhere, non-zero exit on failure, never print secrets.

### 3.10 Tests

- `tests/integration/test_disputes.py` — scenarios 10 and 11, happy path and negative path.
- `tests/integration/test_policy_versioning.py` — scenario 22, plus the negative case (a change with
  no recorded actor is refused).
- `tests/integration/test_finding_lifecycle.py` — **new file**; scenario 23.
- `tests/unit/test_dispute_service.py`, `tests/unit/test_verification_allowlist.py`.
- Extend `tests/contract/test_schema_agreement.py` for rebuttal and disposition.
- Extend `tests/contract/test_openapi_current.py` coverage via the regenerated document.

### 3.11 Documentation

- `docs/api.md` — the dispute endpoints.
- `docs/implementation/roadmap.md` — Phase 3 status and per-item checks.
- `docs/implementation/task-checklist.md` — step 11.
- `docs/implementation/acceptance-tests.md` — check boxes only where a named test passes.
- `docs/operations.md` — how to file a rebuttal and read history.

## Hard requirements

- Append-only. No update or delete path for findings, rebuttals, dispositions, verification jobs, or
  policy versions. Supersession links only.
- Every state change goes through `FindingLifecycle`, so the graph, the capability seam, and the
  audit trail stay the single source of truth.
- No stubs, no `pass  # later`, no endpoint returning placeholder data.
- Run and report real output for `uv run pytest`, `uv run mypy`, `uv run ruff check .`,
  `uv run ruff format --check .`, and `uv run alembic upgrade head`.
- If a spec requirement cannot be met in this run, say so explicitly rather than faking it.

## Reconciliation with the staged prompt

`.agent-prompts/phase3.md` is the staged task prompt for this phase. It is broadly correct and its
scope (rebuttals, verification, dispositions, escalation, `nyx audit verify`) is adopted here. Four
points where it has drifted from the code need to be settled before it is handed to an implementing
agent:

1. **It says to extend a Phase 2b history endpoint.** No history endpoint exists. Phase 2b shipped
   only `GET /api/v1/findings` and `GET /api/v1/findings/{finding_id}` (`src/nyx/api/routes/findings.py`,
   70 lines, read-only). `GET /api/v1/findings/{id}/history` is new work in Phase 3, not an extension.
2. **It assigns acceptance scenario 16 to Phase 3.** Scenario 16 ("a verification tool cannot execute
   an arbitrary command supplied by an untrusted event") is owned by Phase 4 in `acceptance-tests.md`
   and `roadmap.md`. Phase 3 must *not create the hole* — the allowlist is mandatory — but the
   scenario's owning test (`tests/security/test_verification_allowlist.py`) is Phase 4 work. Doing it
   here would mean checking a Phase 4 box from Phase 3, which the ownership rule forbids.
3. **It names scenario 22 but never specifies the policy-version store.** `policy_version` is a
   required field on `Disposition`, and the prompt lists scenario 22 under exit criteria, but no work
   item builds the thing that records a version *and who changed it*. `PolicyRegistry` is in-memory
   and records no change history at all. Work item 3.4 exists to close that gap.
4. **It has no storage or migration work item.** Four new tables are needed. Without an explicit item
   an implementing agent can end up with models and no persistence.

Adopted from the prompt without change: the append-only requirement, the independence rule for
verification (re-run a *different* critic where one exists, and record the limitation where none
does), the no-op notification hook, the Hypothesis interleaving property test, and the audit-chain
trust-limit wording.

## Risks and open questions

- **Supersession semantics.** "Every new disposition supersedes but never deletes the previous one"
  implies a chain with a derived head. The plan treats the head as derived from `decided_at`, not
  stored as a mutable pointer. If a concurrent writer is ever expected, that derivation needs a
  unique constraint on `(finding_id, decided_at)` or an explicit sequence column.
- **Verification allowlist scope.** Only allowlisted checks may run. The plan does not attempt to make
  the allowlist configurable at runtime; a check is code, registered at import.
- **Scenario 23 ownership.** The behaviour is already unit-tested. Writing the integration file is
  bookkeeping, not new behaviour — but it must be written, or the box stays honestly unchecked.
- **Scenario 24 is a Phase 2 scenario.** Wiring C-010 as a confirmation gate is optional here. If it
  is done, it must be called out in the Phase 3 report as closing a Phase 2 gap.
