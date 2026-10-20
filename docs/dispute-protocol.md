# The NYX dispute protocol

**Source:** `docs/SPEC.md` §5.7 (rebuttal and disposition), §6 (lifecycle), §7 (evidence protocol).
**Status:** implemented in Phase 3. Acceptance scenarios 10, 11, 22, and 23 pass.

This document describes the challenge loop end to end: what a finding can be challenged with, what
an independent verification can and cannot establish, who may record an outcome, and what the history
looks like afterwards.

## The loop

```text
a critic raises a finding
        │
        ▼
   detected ──► evidence_pending ──► challenged ──► rebuttal_pending
        │                                                │
        │                                                ▼
        │                                    verification_pending
        │                                                │
        │                        ┌───────────────────────┼───────────────────────┐
        │                        ▼                       ▼                       ▼
        │                   confirmed              rejected              inconclusive
        │                        │
        ▼                        ▼
   rebuttal submitted      remediation_pending ──► fixed_pending_verification ──► closed
```

Every arrow is a transition in the SPEC §6 graph, and every one of them is validated by the same
state machine. The dispute protocol never assigns a status directly — it calls
`FindingLifecycle.transition`, so the graph, the capability seam, and the audit trail stay the single
source of truth.

## 1. Filing a rebuttal

A rebuttal is a challenge. It carries the challenger's identity, an argument, the counterevidence it
attaches, and the outcome the challenger wants.

```
POST /api/v1/findings/{finding_id}/rebuttals
{
  "submitted_by": "sys_ghost_factory_demo",
  "argument": "The attached report covers commit abc123, not def456.",
  "evidence_ids": ["ev_ghost_002"],
  "requested_outcome": "withdrawn"
}
```

Four rules govern it:

1. **It appends.** The finding's title, summary, reasoning, and evidence links are never rewritten.
   Only its status pointer moves and its accumulated `counterevidence_ids` grow.
2. **It must cite inspectable evidence.** An unknown evidence id is rejected with
   `UNKNOWN_EVIDENCE_ID` before anything is stored. A challenge citing evidence nobody can inspect is
   not a challenge NYX can adjudicate.
3. **`requested_outcome` is a request, not a verdict.** `no_change` is a legitimate value meaning "we
   disagree, and here is why the finding should stay".
4. **The finding walks the canonical path.** The SPEC graph has no direct edge into
   `rebuttal_pending`, so a finding passes through `evidence_pending` and `challenged` first. A
   freshly raised finding and an already-challenged one arrive by the same route.

A finding already past rebuttal (`verification_pending` or later) has no edge back and stays where it
is. The rebuttal is still recorded, because a challenge is history even when it cannot reopen the
finding.

## 2. Independent verification

Verification re-examines a finding. It does **not** decide anything.

```
POST /api/v1/findings/{finding_id}/verification-jobs     {"check_id": "critics.rerun-linked"}
POST /api/v1/findings/{finding_id}/verification-jobs/run
GET  /api/v1/verification-jobs/{job_id}
```

**The allowlist is the safety mechanism.** A check that is not registered cannot run, and an
unregistered id is refused *before* anything executes. That is what makes SPEC §17.2 scenario 16
true: untrusted event input never reaches a command line. Checks are code, registered in
`nyx.disputes.verification` — there is no discovery and no runtime configuration of what may execute.

**Independence is recorded, never implied.** Re-running the critic that raised a finding does not
corroborate it; the evaluator shares the original's blind spot. Every check returns an `independence`
statement and every job stores it as a queryable column, so a reader can tell corroboration from
reproduction. The built-in check states plainly that it is a reproduction check.

**Failure is an outcome, not a corruption.** A job that raises, times out, or is refused is recorded
as `failed` or `refused`, never rewritten into a success. The finding is not touched — only an
authorised disposition moves it.

## 3. Recording a disposition

A disposition is the authorised outcome of a finding.

```
POST /api/v1/findings/{finding_id}/dispositions
{
  "outcome": "rejected",
  "rationale": "The premise that verification was missing does not hold.",
  "decided_by": "principal_operator",
  "policy_version": "1.0",
  "counterevidence_ids": ["ev_ghost_002"]
}
```

Rules:

1. **Capability is enforced by the lifecycle machine, not the service.** A principal without the
   disposition capability is refused with `CAPABILITY_REQUIRED`. The checker is bound to application
   state and defaults to deny-all until Phase 4 wires real authorisation.
2. **A rationale is mandatory.** SPEC §6 requires a rejection to explain which premise failed.
3. **A challenged finding must name what was weighed.** A rebuttal is an answerable challenge: a
   challenged finding cannot be dismissed as `rejected` or `inconclusive` without recording the
   counterevidence considered. An *unchallenged* finding can still be rejected on its own terms —
   the critic may simply have misread the evidence — so the rule does not force a meaningless field.
4. **It supersedes by linking.** `supersedes_disposition_id` names the disposition it replaces. The
   previous row is left exactly as it was, so the full chain is reconstructable.
5. **A verdict is reachable only from `verification_pending`**, so the service walks the canonical
   path there first. A finding already past that point has no edge back, and the verdict is validated
   against its actual state — a confirmed finding cannot simply be rejected.

## 4. Policy versions

Every disposition records the policy version that decided it, so a later policy change cannot
retroactively alter what was decided.

```
GET /api/v1/policies/versions
```

`PolicyVersionStore.register` makes a rule live in the registry **and** records who changed it, in
one call. A policy cannot become active without a recorded change. Registering an already-recorded
version is refused rather than replaced, because history is not editable.

```
POST is not exposed: registering a policy change is a code change, not an API call.
```

## 5. Reading the history

```
GET /api/v1/findings/{finding_id}/history
```

Returns the original finding, every rebuttal, every verification attempt, and the full disposition
chain. Nothing is ever removed, so the history is a complete answer to "what happened to this
finding, and why".

## Worked example

Ghost Factory claims a feature is complete for commit `abc123`. C-001 raises a finding because the
only attached test run targets `def456`.

1. **The finding is raised** by C-001 and sits in `detected`.
2. **Ghost Factory files a rebuttal** citing `ev_ghost_002`, a test report for `abc123`, and requests
   withdrawal. The finding walks to `rebuttal_pending`; its title, summary, and reasoning are
   unchanged, and `ev_ghost_002` is now in its accumulated `counterevidence_ids`.
3. **A verification job is requested** for `critics.rerun-linked` and run. It re-runs C-001 against
   current records, writes a new `CriticRun`, composes no second finding, and reports `refuted` — the
   finding no longer reproduces. Its `independence` states that this is a reproduction check, not
   independent corroboration.
4. **An authorised operator records a disposition** of `rejected`, naming `ev_ghost_002` as the
   counterevidence weighed and citing policy version `1.0`. The finding moves to `rejected`.
5. **The history is read** and shows the original finding, the rebuttal, the verification job, and
   the disposition, in the order they happened.

If instead the operator had tried to dismiss the challenge without naming any counterevidence, the
lifecycle machine would have refused the transition, and the finding would have stayed exactly where
it was.

## What Phase 3 deliberately does not do

- **No authentication.** The actor is taken from the request body, so every dispute record is
  currently self-asserted. Phase 4 replaces this with a verified principal.
- **No remediation.** Recording that something was fixed is not the same as fixing it. The MVP
  performs no external action (ADR 0004, safety invariant 6).
- **No closing a finding because the target says so.** `fixed_pending_verification` is not `closed`;
  closure requires a verification result or an explicitly authorised waiver.
