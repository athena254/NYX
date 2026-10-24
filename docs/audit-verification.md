# NYX audit verification

**Status:** implemented (Phase 4, slice 4.2).
**Contract:** `docs/SPEC.md` §5.8 and §17.2 scenario 17; ADR 0003.

Every audit entry records a digest of its own fields plus the digest of the entry before it. The
chain therefore makes accidental edits, partial writes, and reordering visible.

```bash
nyx audit verify              # human-readable report
nyx audit verify --json       # one JSON document, for callers
nyx audit checkpoint          # append an external anchor for the current head
nyx audit verify --against FILE   # is the stored history the history I anchored?
```

Exit codes: `0` on a clean chain, `1` on divergence. Never a traceback. A checkpoint comparison that
refuses -- no file, a chain shorter than the anchor, a malformed line -- also exits `1`.

## What is checked

1. `sequence` starts at 1 and increments by exactly 1.
2. Each entry's `prior_entry_hash` equals the previous entry's `entry_hash`.
3. Each entry's `entry_hash` recomputes from its stored fields.

The verifier walks the whole chain in pages and reports the **first** divergence, then stops.

## External checkpoints (ADR 0012)

The three checks above are all checks an actor who rewrites the whole database can satisfy: change
every `reason`, recompute every `entry_hash`, and `nyx audit verify` reports a clean chain over
records that are not the ones that were written.

A checkpoint is the out-of-band anchor that closes that case:

```bash
nyx audit checkpoint                      # writes <data_dir>/audit-checkpoint.jsonl
cp data/audit-checkpoint.jsonl /safe/place/
# ...later, after any suspicion that the store was touched...
nyx audit verify --against /safe/place/audit-checkpoint.jsonl
```

Each record is `{schema_version, sequence, head_hash, recorded_at, entries_checked}`, appended as
one JSON line. `--against` compares the stored chain's head at that sequence against the anchored
digest. A fully recomputed rewrite no longer matches, because the attacker would have had to forge
the checkpoint too.

**Store the checkpoint where the database's writer cannot reach it.** The default path is next to
the database, which is a convenience: an actor who can rewrite the database can very likely rewrite a
file in the same directory. The default is not the strong choice, and the strong choice is to copy
the file elsewhere.

**A refusal is not a pass.** An absent file, an empty file, a chain shorter than the checkpoint, a
checkpoint ahead of the chain, and a malformed line are all explicit failures with a stable reason
code (`no_checkpoint`, `checkpoint_truncated`, `checkpoint_sequence_beyond_chain`,
`checkpoint_hash_mismatch`, `malformed_checkpoint`). The command reports red, never a silent success.

## Startup behaviour

`NYX_AUDIT_VERIFY_ON_STARTUP=true` (the default) verifies the chain on boot and logs a warning on
divergence. **It does not refuse to start.** A diverged chain is a finding for an authorised human,
not a reason to take the service down — and a service that refuses to boot because its own audit
trail is damaged is a service an operator cannot inspect.

`nyx doctor` reports the chain as one of its checks.

## What this does not establish

The chain is a local integrity control, not a signature, not a transparency log, and not a claim of
tamper-proof storage. It detects mutation **within the stored sequence**. An attacker who can
rewrite the whole database and recompute every hash defeats it — as does an administrator with
write access to the database file, which is outside NYX's control. Deletion of the underlying
database is not prevented and is not claimed to be.

Say so when reporting a result. A "verified" chain means *no mutation was detected*, not *the
records are authentic*.

## Overlap with C-010, recorded rather than hidden

The NYX self-consistency critic (`src/nyx/critics/c010_nyx_self_consistency.py`) performs the same
three checks inside its `_check_audit_chain`, and reports **every** violation rather than the first.
The two surfaces are not duplicates with different answers — they are different *outputs* of the
same hash function:

| Surface | Where it runs | What it produces |
| --- | --- | --- |
| `AuditVerifier` (`src/nyx/audit/verifier.py`) | operator command, startup check, `nyx doctor` | first divergence, exit code |
| C-010 `_check_audit_chain` | the deterministic critic pack | a finding against the store |

Both import `entry_hash` from `src/nyx/audit/chain.py`. There is one implementation, so the writer
and both verifiers cannot drift apart.

C-010 is **not** enforced as a gate on `confirmed` findings (scenario 24 is unchecked), so it does
not replace `nyx audit verify`: the critic runs when the pack runs, and the command runs when the
operator asks.
