# ADR 0012 — Anchor the audit chain with an external checkpoint

**Status:** accepted, 24 October 2026.

## Context

The audit hash chain (`src/nyx/audit/chain.py`, verifier in `src/nyx/audit/verifier.py`) is a
**local** integrity control, and SPEC §5.8 says so: *"If a hash chain is implemented, document its
trust limits; do not describe it as tamper-proof against a privileged attacker who can rewrite the
entire store."*

That limit is not hypothetical. The chain detects mutation **within** the stored sequence: a changed
reason, a deleted middle row, an overwritten `entry_hash`. An actor who rewrites the whole database
and recomputes every digest produces a chain that verifies cleanly, because every check the verifier
makes is a check the attacker can satisfy. `nyx audit verify` reports `ok`, and the records are not
what they were.

The caveat plan (`docs/implementation/caveats-plan.md`, slice H3) recorded this as one of the
limitations a user would meet. The question was what to do about it without pretending to solve a
problem that has no in-process solution.

## Decision

**Anchor the chain out-of-band, at a path the operator chooses.**

`src/nyx/audit/checkpoint.py` adds two operations and no new storage:

- **`nyx audit checkpoint`** appends one JSONL record -- `{schema_version, sequence, head_hash,
  recorded_at, entries_checked}` -- for the current chain head. Default path is
  `<data_dir>/audit-checkpoint.jsonl`; `--file` overrides it.
- **`nyx audit verify --against FILE`** walks the stored chain to the checkpointed sequence and
  compares the entry hash. A rewrite no longer verifies.

A checkpoint is a claim about *the history I recorded*, held somewhere the chain's own writer does
not control. That is the entire mechanism. There is no signature, no key, no transparency log, and no
network call.

**A refusal is not a pass.** A missing file, an empty file, a chain shorter than the checkpoint, a
checkpoint ahead of the chain, and a malformed line are all explicit failures with a stable reason
code. A checker that treats "there is no checkpoint" as "verified" is worse than no checker, because
it manufactures the one thing it was added to provide. `compare_against_checkpoint` returns a red
`CheckpointComparison` for an absent or empty file rather than raising, because that is a *verdict
about the check*, not an input error; an unreadable or malformed file raises `CheckpointError`,
because that is an input error, and the CLI reports the two differently.

**The module records its own limits in three places** -- the module docstring, `CheckpointComparison`,
and the CLI help -- because the failure mode this guard exists to prevent is precisely the one an
over-claim would hide.

## Consequences

- **The chain's documented weakness is closed for the rewrite case, and only that case.** A
  checkpoint stored *next to the database* is a convenience, not a guarantee: an actor who can
  rewrite the database can very likely rewrite a file in the same directory. The default path is the
  convenient one; the docs say the strong one is "copy it somewhere else".
- **Deletion is still not detected if the checkpoint is also deleted.** That is stated, not hidden.
- **Two surfaces answer two different questions.** `nyx audit verify` asks *is the stored history
  internally consistent*; `nyx audit verify --against FILE` asks *is the stored history the history I
  anchored*. They cannot produce each other's answer, so they are separate functions with separate
  output shapes.
- **No migration, no new dependency, no schema change.** The checkpoint file is not NYX storage.
- **`nyx audit checkpoint` is a write to a file, not to the store.** It appends to a JSONL file and
  records no audit entry of its own, so a checkpoint cannot be mistaken for an audit event.

## Reversibility

Fully additive. Removing `src/nyx/audit/checkpoint.py` and the two CLI options leaves the chain, the
verifier, and every migration untouched. No stored record refers to a checkpoint.
