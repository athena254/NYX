# Acceptance tests — SPEC §17.2

All 24 acceptance scenarios are listed below. A box may be checked only when the named test exists,
is run in CI or locally, and passes against the implementation. After the Phase 3 run, scenarios 1–12,
14, 18, 22, and 23 are checked with the exact tests named below; all others remain unchecked.

**Scenario 24 note (21 October 2026):** scenario 24 is now **checked**. C-010 is enforced as a gate
on `confirmed` and `partially_confirmed` by `nyx.findings.lifecycle` (`_SELF_CONSISTENCY_GATED` plus
the `self_consistency_gate` seam), so a finding that fails the self-consistency check cannot be
confirmed. Verified by `tests/unit/test_scenario_24_self_consistency_gate.py` (7 tests) and
`tmp/probes/probe_scenario_24_gate.py` (6 checks against a real migrated database, written outside
the project's test helpers). The Phase 2 and Phase 4 notes below record why it was unchecked then;
they are history, not current status.

**Ownership rule:** the phase column is the first phase that must satisfy the scenario. A scenario
marked “re-validated” must still pass in the later phase, but the earlier phase owns the first passing
test.

**Phase 2 note (20 October 2026):** scenarios 4–9 and 12 are checked after the Phase 2a/2b runs, verified
with `uv run pytest` (185 tests passing) against real Alembic-migrated SQLite databases, `uv run mypy`
(strict, 81 files), and `uv run ruff check .` (121 files). Scenario 24 stays **unchecked**: the C-010
critic exists, is registered, and is tested in both directions by `tests/unit/test_critic_c010.py`, but
it is not enforced as a gate on `confirmed`, so the invariant "every confirmed finding passes the NYX
self-consistency critic" is not yet upheld by any code path.

**Phase 3 note (20 October 2026):** scenarios 10, 11, 22, and 23 are checked. Scenario 16 stays
unchecked on purpose: it is Phase 4-owned, and checking it here would break the ownership rule.
Phase 3's obligation is the narrower one -- do not create the hole -- and the allowlist is
unit-tested to refuse an unregistered check before anything executes.

**Phase 4 note (21 October 2026):** scenarios 13, 15, 16, 17, 19, 20, and 21 are checked, and 14
and 18 still pass. Scenario 19 is checked for the **stub** path only; the live provider adapter is
Phase 6. Scenario 24 remains unchecked and is not Phase 4's: C-010 is still not enforced as a gate
on `confirmed`.

Every Phase 4 box above was checked by running the project's own gates (`uv run pytest`, `uv run
mypy`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run alembic upgrade head`) plus a
probe written **outside** the project's test helpers and driven against a real migrated database.
A module's own tests were written by whoever wrote the module; one bad shared assumption hides the
same bug on both sides. The probes are in `tmp/` (gitignored) and are named `probe_*.py`.


**Phase 1b note (19 October 2026):** scenarios 1, 2, 3, 14, and 18 were run with
`uv run pytest -q` (89 tests passing) against a real Alembic-migrated SQLite database. Two test files
were renamed from the Phase 0 plan and the table below records the current paths. Scenario 14's
"before model processing" clause is met by ordering plus the fact that Phase 1 makes no model calls;
model-path revalidation remains Phase 4/6 work. Scenario 18 covers an unreachable database and
migrations behind head; provider outage is Phase 4/6. Audit-chain verification (scenario 17) is still
unimplemented.

| # | Acceptance scenario | Phase | Owning test file | Status |
| --- | --- | --- | --- | --- |
| 1 | An event can be ingested, retrieved, and traced to its source system. | 1 | `tests/integration/test_api_contract.py` | [x] |
| 2 | Replaying an identical event is idempotent. | 1 | `tests/integration/test_ingestion_idempotency.py` | [x] |
| 3 | An event with invalid schema is rejected with a structured error and no partial write. | 1 | `tests/integration/test_ingestion_validation.py` | [x] |
| 4 | A completion claim linked to a passing test run on the same commit does not trigger C-001. | 2 | `tests/integration/test_findings_readback.py` | [x] |
| 5 | A completion claim linked only to a different commit triggers a scoped finding. | 2 | `tests/integration/test_findings_readback.py` | [x] |
| 6 | Missing verification creates a verification gap, not an assertion that the implementation is defective. | 2 | `tests/unit/test_critic_c001.py` | [x] |
| 7 | Contradictory claims for different versions are not automatically treated as contradictory. | 2 | `tests/unit/test_critic_c002.py` | [x] |
| 8 | Stale evidence cannot silently verify a newer artifact. | 2 | `tests/unit/test_critic_c003.py` | [x] |
| 9 | A finding cannot become confirmed without required evidence and rationale. | 2 | `tests/unit/test_critic_c001.py` | [x] |
| 10 | A rebuttal appends to history and does not overwrite the original finding. | 3 | `tests/integration/test_disputes.py` | [x] Phase 3: a rebuttal appends and leaves the finding otherwise unchanged. |
| 11 | Counterevidence can lead to rejection or an inconclusive disposition. | 3 | `tests/integration/test_disputes.py` | [x] Phase 3: counterevidence leads to rejection or inconclusive, and the negative case is covered. |
| 12 | Invalid lifecycle transitions are rejected without changing stored state. | 2 | `tests/unit/test_finding_lifecycle.py` | [x] |
| 13 | A caller cannot read restricted evidence without the required capability. | 4 | `tests/security/test_evidence_access.py` | [x] Phase 4: 7 tests, including 401 with no credential and 403 when the body claims another identity. |
| 14 | Secrets in incoming payloads are redacted before persistence and model processing. | 1 (re-validated 4) | `tests/unit/test_redaction.py`, `tests/integration/test_redaction_persistence.py` | [x] |
| 15 | Prompt injection embedded in evidence cannot grant tool access or change trusted policy. | 4 (re-validated 6) | `tests/security/test_prompt_injection.py` | [x] Phase 4: the fence cannot be closed from inside, and policy resolution reads only from the registered rule set. |
| 16 | A verification tool cannot execute an arbitrary command supplied by an untrusted event. | 4 | `tests/security/test_verification_allowlist.py` | [x] Phase 4: an unregistered check is refused before anything runs, and a `src/` grep guard fails the build if `subprocess`/`os.system`/`shell=True`/`eval(`/`exec(` appears. |
| 17 | Audit history detects accidental record mutation within the implemented threat model. | 4 | `tests/integration/test_audit_integrity.py` | [x] Phase 4: 10 tests mutating real rows; the non-claim (a full-database rewrite) is asserted as wording, not as coverage. |
| 18 | Database/provider outage produces a safe failure and does not report successful ingestion. | 1 (re-validated 4) | `tests/integration/test_readiness_failure.py` | [x] |
| 19 | LLM timeout or invalid structured output does not corrupt finding state. | 4 (stub) / 6 (live adapter) | `tests/unit/test_llm_critic_failure.py` | [x] Phase 4 stub path: 8 tests asserting on the `findings` and `critic_runs` tables. Live adapter remains Phase 6. |
| 20 | `nyx doctor`, health, and readiness correctly report dependency state without leaking secrets. | 4 | `tests/integration/test_health_and_doctor.py` | [x] Phase 4: a stopped database is a 503 with no traceback; doctor prints `***redacted***` and no secret value or prefix. |
| 21 | API and CLI expose consistent status and error semantics. | 4 | `tests/contract/test_api_cli_consistency.py` | [x] Phase 4: the families both surfaces expose agree; auth/capability/rate-limit are API-only by design. |
| 22 | A policy change records the new version and who changed it. | 3 | `tests/integration/test_policy_versioning.py` | [x] Phase 3: a policy change records the version, actor, and reason. |
| 23 | A fixed finding remains open until verification or a documented authorised waiver. | 3 | `tests/integration/test_finding_lifecycle.py` | [x] Phase 3: a fixed finding stays open without a verification result or waiver. |
| 24 | Every confirmed finding passes the NYX self-consistency critic. | 2 | `tests/unit/test_scenario_24_self_consistency_gate.py` | [x] Phase 2 obligation, closed 21 October 2026: C-010 is enforced as a gate on `confirmed`/`partially_confirmed` by `nyx.findings.lifecycle`. 7 tests: a consistent finding confirms; a dangling evidence reference, a tampered audit chain, and a missing transition history each refuse and leave the row unchanged; the gate is substitutable but has no off switch; ordinary progress is unaffected. |

## Phase gate view

| Phase | Acceptance scenarios |
| --- | --- |
| 1 — Evidence and event spine | 1, 2, 3, 14, 18 |
| 2 — Deterministic critic engine | 4–9, 12, 24 (all passing) |
| 3 — Disputes and verification | 10, 11, 22, 23 (all passing) |
| 4 — Security and operational hardening | 13, 14, 15, 16, 17, 18, 19, 20, 21 (all passing) |
| 5 — Integrations | No numbered scenarios; adapter contract and disablement tests |
| 6 — Optional model critics and calibration | 19 (live model path), 15 (model path), calibration thresholds |

## Evidence required to check a box

For each scenario, the change that checks it must include:

1. The test file named above, or a documented rename that updates this table.
2. A positive assertion for the required behaviour.
3. At least one negative or boundary assertion where the scenario implies one.
4. The exact command run and its real output in the change report.
5. A note of any part of the scenario that is still not covered.

Do not check a box because the test file exists, because an import succeeds, or because a related
module is present. Do not check a box based on a manual demonstration that is not reproducible.
