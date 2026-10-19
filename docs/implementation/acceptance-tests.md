# Acceptance tests — SPEC §17.2

All 24 acceptance scenarios are listed below and are **unchecked**. Nothing in this repository is
verified by this document. A box may be checked only when the named test exists, is run in CI or
locally, and passes against the implementation.

**Ownership rule:** the phase column is the first phase that must satisfy the scenario. A scenario
marked “re-validated” must still pass in the later phase, but the earlier phase owns the first passing
test.

| # | Acceptance scenario | Phase | Owning test file | Status |
| --- | --- | --- | --- | --- |
| 1 | An event can be ingested, retrieved, and traced to its source system. | 1 | `tests/integration/test_ingestion_api.py` | [ ] |
| 2 | Replaying an identical event is idempotent. | 1 | `tests/integration/test_idempotency.py` | [ ] |
| 3 | An event with invalid schema is rejected with a structured error and no partial write. | 1 | `tests/integration/test_ingestion_validation.py` | [ ] |
| 4 | A completion claim linked to a passing test run on the same commit does not trigger C-001. | 2 | `tests/unit/test_critic_c001.py` | [ ] |
| 5 | A completion claim linked only to a different commit triggers a scoped finding. | 2 | `tests/unit/test_critic_c001.py` | [ ] |
| 6 | Missing verification creates a verification gap, not an assertion that the implementation is defective. | 2 | `tests/unit/test_critic_c001.py` | [ ] |
| 7 | Contradictory claims for different versions are not automatically treated as contradictory. | 2 | `tests/unit/test_critic_c002.py` | [ ] |
| 8 | Stale evidence cannot silently verify a newer artifact. | 2 | `tests/unit/test_critic_c003.py` | [ ] |
| 9 | A finding cannot become confirmed without required evidence and rationale. | 2 | `tests/unit/test_finding_lifecycle.py` | [ ] |
| 10 | A rebuttal appends to history and does not overwrite the original finding. | 3 | `tests/integration/test_disputes.py` | [ ] |
| 11 | Counterevidence can lead to rejection or an inconclusive disposition. | 3 | `tests/integration/test_disputes.py` | [ ] |
| 12 | Invalid lifecycle transitions are rejected without changing stored state. | 2 | `tests/unit/test_finding_lifecycle.py` | [ ] |
| 13 | A caller cannot read restricted evidence without the required capability. | 4 | `tests/security/test_evidence_access.py` | [ ] |
| 14 | Secrets in incoming payloads are redacted before persistence and model processing. | 1 (re-validated 4) | `tests/security/test_redaction.py` | [ ] |
| 15 | Prompt injection embedded in evidence cannot grant tool access or change trusted policy. | 4 (re-validated 6) | `tests/security/test_prompt_injection.py` | [ ] |
| 16 | A verification tool cannot execute an arbitrary command supplied by an untrusted event. | 4 | `tests/security/test_verification_allowlist.py` | [ ] |
| 17 | Audit history detects accidental record mutation within the implemented threat model. | 4 | `tests/integration/test_audit_integrity.py` | [ ] |
| 18 | Database/provider outage produces a safe failure and does not report successful ingestion. | 1 (re-validated 4) | `tests/integration/test_failure_handling.py` | [ ] |
| 19 | LLM timeout or invalid structured output does not corrupt finding state. | 4 (stub) / 6 (live adapter) | `tests/unit/test_llm_critic_failure.py` | [ ] |
| 20 | `nyx doctor`, health, and readiness correctly report dependency state without leaking secrets. | 4 | `tests/integration/test_health_and_doctor.py` | [ ] |
| 21 | API and CLI expose consistent status and error semantics. | 4 | `tests/contract/test_api_cli_consistency.py` | [ ] |
| 22 | A policy change records the new version and who changed it. | 3 | `tests/integration/test_policy_versioning.py` | [ ] |
| 23 | A fixed finding remains open until verification or a documented authorised waiver. | 3 | `tests/integration/test_finding_lifecycle.py` | [ ] |
| 24 | Every confirmed finding passes the NYX self-consistency critic. | 2 | `tests/unit/test_critic_c010.py` | [ ] |

## Phase gate view

| Phase | Acceptance scenarios |
| --- | --- |
| 1 — Evidence and event spine | 1, 2, 3, 14, 18 |
| 2 — Deterministic critic engine | 4, 5, 6, 7, 8, 9, 12, 24 |
| 3 — Disputes and verification | 10, 11, 22, 23 |
| 4 — Security and operational hardening | 13, 14, 15, 16, 17, 18, 19, 20 |
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
