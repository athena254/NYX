# ADR 0010 — Agent-transcript adapters: observe an agent's own log

**Status:** accepted, 24 October 2026.

## Context

The Git/CI adapter (ADR 0008) taught NYX to observe one kind of external record: a CI host's own
account of a run. The next obvious source is the agents themselves. Hermes keeps an append-only
conversation log — sessions, assistant messages, tool calls, tool results — and that log is exactly
the material NYX is built to criticise: claims an agent made about its own work, and the evidence
that was or was not available when it made them.

Two temptations had to be resisted.

**The first is a second ingestion path.** The natural implementation is "read the log, write the
records into NYX". That bypasses the gateway, so it bypasses redaction, schema validation,
identity deduplication, and the per-record audit entry — the whole pipeline that makes a record
admissible. Whatever the adapter reads must arrive as *producer input*, exactly as a human submitter
would send it.

**The second is a fork per agent.** Every supported agent keeps its transcript in a different
format, but they are the same *shape*: an append-only typed event stream in one database, keyed by
a stable (session, ordinal) pair, where assistant text is a candidate claim and a tool call with
its result is candidate evidence. A per-agent copy of the mapping logic would be N near-identical
modules that drift.

## Decision

**One base class, two hooks.** `src/nyx/adapters/agent_transcript/base.py` implements everything
the shared shape has in common — the read-only health probe, the stable identities that make a
re-run a no-op, and the mapping of one normalized `Turn` onto the producer input models — and
leaves exactly two hooks to the subclass:

- `iter_turns()` — read the store and yield `Turn` values;
- `default_db_path()` — where the store lives when no path is configured.

`HermesAdapter` is the first subclass. Adding OpenCode, or any agent with the same shape, is one
subclass and one `register()` call in the shared factory (ADR 0009).

**It is read-only on the source.** The store is opened with a `mode=ro` URI and nothing is ever
written to it (ADR 0004). NYX must never be able to corrupt — or even touch — a live agent's
database, and a read-only handle is the control that makes that a property of the code rather than
an instruction to the operator.

**It is stateless, and idempotency comes from identity.** There is no cursor and no session. Every
emitted event, claim, and evidence item carries a deterministic `id` and `producer_event_id`
derived from the transcript's own ids, so the gateway's identity-based deduplication makes a second
fetch a set of duplicates rather than new rows. This is what makes "run it every N minutes" safe,
and it is the same property ADR 0009 requires of the watcher.

**An error is not an empty result.** A missing store raises `TranscriptError`, which propagates out
of `fetch_for_ref`. "The adapter is down" is an error a caller must retry, never an observation
that the agent said nothing — the same rule the Git/CI adapter already follows.

**It sits behind the ingestion boundary, and the boundary is a build gate.** The package imports
only `nyx.adapters`, `nyx.domain`, `nyx.ingestion`, and the standard library. `normalize()` returns
producer *input* models, never domain models, so its output inherits redaction, validation,
deduplication, and audit for free. `tests/contract/test_adapter_registry.py` fails the build on a
violation.

## Consequences

- **The adapter cannot become a second ingestion path.** It produces input, and only the gateway
  turns input into records. A future agent adapter that wants to bypass that has to edit a
  contract-tested boundary, not add a module.
- **A new agent is cheap and cannot drift.** Two methods, one file, one register call. The shared
  mapper — turn to event, claim, evidence — is written once.
- **A missing transcript store is an error, not silence.** Callers can distinguish "the agent said
  nothing" from "the adapter could not read the agent".
- **The Hermes store is observed, never written.** The read-only URI is the control; the adapter's
  health probe opens it read-only as well, so the probe itself cannot mutate the source.
- **`hermes` is registered by default and enabled by configuration**, like every other adapter
  (ADR 0008): the code is installed, the deployment decides whether it runs.

## Reversibility

Additive. Removing `src/nyx/adapters/agent_transcript/` and the two factory registrations leaves the
Git/CI adapter, the boundary gate, and every migration untouched. No stored record refers to an
agent-transcript adapter.
