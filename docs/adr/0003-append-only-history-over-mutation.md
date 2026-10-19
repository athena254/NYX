# 0003. Append-only history over mutation

**Status:** Accepted (Phase 0, 19 October 2026).

## Context

NYX publishes findings, receives rebuttals, and records dispositions. The SPEC requires that
rebuttals and new evidence append to history and never erase prior records, and that every new
disposition supersedes but never deletes its predecessor. It also requires an append-only audit
history for material transitions. A critic that can silently rewrite its own accusations cannot be
audited, and a target cannot be asked to trust a record that changes underneath it.

## Decision

- **Findings, rebuttals, dispositions, evidence records, and audit entries are append-only.**
- A disposition is a new record that references the one it supersedes through
  `supersedes_disposition_id`; the prior record is never updated or deleted.
- A rebuttal is a new record linked to its finding; it never overwrites the finding, its evidence, or
  a prior rebuttal.
- New evidence is a new item, not an edit of an existing item. Correcting an evidence item means
  registering a corrected item with its own ID and recording the relationship.
- Lifecycle state is derived from the latest valid transition, and every transition is recorded as an
  audit entry with actor, reason, and time.
- Redaction under a legal obligation is handled as an explicit, audited operation that may replace
  payload content with a tombstone, not as an ordinary update. See `docs/privacy-and-retention.md`.
- Storage rejects in-place updates and deletes for these record classes at the application layer and,
  where practical, with database constraints.

## Consequences

- Full history is available for audit, appeal, and false-positive/negative analysis.
- Storage grows monotonically. Retention classes and redaction are required to bound sensitive data
  rather than deletion of the historical record.
- Queries that need the current state must select the latest record or transition, which is a small
  extra read cost and must be encapsulated in repositories.
- Correcting a mistake never rewrites the past; it adds a superseding record and, for a mistaken NYX
  accusation, a `withdrawn` disposition.
- A privileged database administrator can still rewrite the underlying store; append-only application
  behaviour is not presented as tamper-proof against that actor.

## Alternatives considered

- **Mutable rows with an audit side table.** Easier current-state reads, but the authoritative record
  and the history can diverge, and a bug or a malicious actor can rewrite the row while leaving a
  plausible-looking audit entry.
- **Soft delete flags.** Preserves rows but allows silent semantic mutation and is easy to bypass.
- **Cryptographic hash chain as the primary guarantee.** Adds complexity and a false sense of
  immutability; a local chain does not stop an administrator who can rewrite the whole store. A
  documented hash chain may be added for accidental-mutation detection, but it does not replace
  append-only behaviour.
- **Event sourcing with full state rebuild.** Powerful but heavier than the MVP needs and harder to
  query for findings and dispositions with the required filters.
