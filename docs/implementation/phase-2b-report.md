# Phase 2b checkpoint report

**Date:** 20 October 2026
**Repository:** `D:\NYX`, branch `master`
**Checkpoint commit:** `44a0351` (critics + policy engine), preceded by `4c43628` (lifecycle machine)

This is the verified record of the Phase 2b stopping point. It records what was actually run and what
it returned, not what was intended.

## Scope

Phase 2b delivered the second half of SPEC §8's minimum critic pack (C-005..C-010), the built-in
versioned policy engine, and the finding lifecycle state machine that the dispute protocol will be
built on. Phase 2a delivered the critic engine, the finding composer, and C-001..C-004.

## Commands run and real output

```text
$ uv run pytest -p no:randomly
185 passed, 1 warning in 32.70s
exit=0

$ uv run mypy
Success: no issues found in 81 source files
exit=0

$ uv run ruff check .
All checks passed!
exit=0

$ uv run ruff format --check .
121 files already formatted
exit=0
```

The suite count rose from 140 (Phase 2a) to 185 (Phase 2b).

## Registry state

- 9 critics are registered at import time.
- 10 are registered after `create_app()` (C-010 included), because it is storage-backed.

## Defect found and fixed during this checkpoint

The suite was **red** on the first run of the checkpoint verification: one Hypothesis property test
failed with `DeadlineExceeded` — `test_no_transition_sequence_reaches_an_invalid_state` took 261ms
against the 200ms default wall-clock deadline, and on a retry took 77ms.

The test copies a migrated SQLite database per example and drives up to ten transactions, so the cost
is file I/O, not the state machine. The 200ms deadline is the wrong tool, not a code defect. Fixed by
applying the same convention already used in `tests/unit/test_ingestion_properties.py`:

```python
@hypothesis_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
```

After the fix the suite passes deterministically (repeated runs of the same file stay green).

## Verified by direct inspection, not by accepting the claim

### C-005 — policy violation

Reproduced the finding and confirmed it names the policy ID and its version, carries the input record
payload, and sets `category=policy_violation`. The engine resolves three rules: self-approval is
`violated`, a distinct approver is `satisfied`, and an unknown policy ID raises rather than
defaulting to pass.

### C-009 — evidence conflict

Returns `INCONCLUSIVE`, not a finding, on a conflicting-evidence case. This matters because a critic
that guesses on ambiguous evidence is worse than one that declines to answer.

### C-006 — risky action without authorisation

Seven controls against three positive tests. An ordinary action, a blank `approval_ref`, and the
substring "undeploy" all stay quiet — no false positives. Its findings state in their own
`limitations` that NYX performed nothing, honouring safety invariant 6.

### C-010 — NYX self-consistency

Tested in both directions: a fully consistent store stays quiet, and a store with a missing lifecycle
transition or a tampered audit entry fires. Registered through `register_storage_backed()` so both the
API factory and the CLI pass the session factory they already built. Constructed without one it
returns an `ERROR` result, never a silent pass.

## Judgment calls

1. **C-006 was written directly, not by a subagent.** The subagent looped twice without writing a
   byte. What fixed C-005 was specifying the exact API instead of leaving the design open; the
   subagent given a prescriptive brief produced clean work in eight minutes, while the ones given
   design freedom looped. Prescriptive briefs are now the rule for this project.

2. **C-010 needed real wiring, not just registration.** It audits NYX's own store, so handing it a
   separately-built session factory would let it inspect a different database than the live one —
   worse than not running it. It is registered through `register_storage_backed()`, which both the API
   factory and the CLI call with the factory they already built.

## Open items at this checkpoint

- **Acceptance scenario 24 is partially met.** C-010 exists and is tested in both directions, but it
  is not enforced as a gate on `confirmed`. A finding can currently be confirmed without C-010 being
  consulted. The box in `acceptance-tests.md` stays unchecked.
- The capability seam is still `DenyAllCapabilityChecker` until Phase 4 wires real authentication.
- Audit chain verification (`tests/integration/test_audit_integrity.py`) is Phase 4.
- `critics/scheduler.py` is not implemented; critics run on demand via the run-critics endpoint.
- `tests/integration/test_findings_api.py` is not written.

## Next

Phase 3 — the dispute protocol: rebuttals, verification jobs, and dispositions. This is what the
lifecycle machine was built to support. See `roadmap.md` Phase 3 and `acceptance-tests.md` scenarios
10, 11, 22, and 23.
