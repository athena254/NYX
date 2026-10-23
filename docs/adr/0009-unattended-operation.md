# 0009. Unattended operation: `nyx watch`

**Status:** Accepted (23 October 2026).

## Context

NYX has no scheduler. Every operation — ingest, critic run, verification — happens only when a
human invokes the CLI or calls the API. That is a library, not a service: nothing watches anything
unless someone presses the button.

Running NYX against its own repository made a second gap visible. **The Phase 5 adapter is never
called by any production code.** `grep -rn "fetch_for_ref" src/` returns only the adapter's own
definition and its docstring; the sole constructor lives inside `nyx doctor`
(`src/nyx/cli/main.py`), which builds the registry purely to print `registered but not
constructible`. `grep -c adapter src/nyx/api/app.py` returns **0**. The adapter is a dead end:
built, tested, documented, and driven by nothing.

SPEC §1.1 lists twelve required first-release outcomes and none of them is "run unattended"; SPEC
§4.1's required-module list has no scheduler. So this is a **new capability**, not the
implementation of something specified. That matters: it must not be dressed up as fulfilling a
requirement, and it must be possible to say honestly that it is an extension.

Four constraints shape the decision:

1. **NYX is local-first and deliberately boring.** SPEC §2 asks for "boring, well-supported
   dependencies and a local-first default". A scheduler is a dependency; a supervisor is not.
2. **Adapters are producers behind the ingestion gateway, and they may not judge** (ADR 0008).
   A watcher is the same shape: read, normalise, ingest, criticise — never decide.
3. **SPEC §3's safety invariants and Phase 7's "the model is the interface, not the judge" both
   exist because an unattended writer is an unattended authority.** Anything that writes without a
   human in the loop has to be scoped very carefully.
4. **Idempotency is what makes a timer safe.** The ingestion gateway already deduplicates by
   identity. A watcher that re-fetches an unchanged ref must produce no new rows.

## Decision

- **`nyx watch` is a CLI command, not a background thread inside `serve`.** One process, one loop,
  any supervisor. NYX stays stateless: `serve` remains a request/response process with no timer, no
  in-process scheduler, and no lifecycle of its own. A daemon embedded in the API server would
  couple the HTTP surface to the host's process supervision for no gain.
- **The watcher is read-only.** It fetches from enabled adapters, ingests through the ordinary
  gateway, and runs the deterministic critic pack. It **never** files a rebuttal, **never** records
  a disposition, and **never** changes a lifecycle state. Findings it produces are ordinary
  findings in `detected` state; a human still closes them. This is the same rule ADR 0004 and
  Phase 7 already apply to adapters and to the conversational layer.
- **A cycle is idempotent and safe to repeat.** Re-fetching an unchanged ref yields zero new
  rows, because the gateway's identity-based deduplication rejects the replays. This is what makes
  "run it every N minutes" safe rather than noisy.
- **One failure never kills the loop.** A down adapter is a degraded cycle, recorded and
  continued — the same posture as Phase 5's disablement test, where an unreachable adapter left
  `GET /ready` at 200. A watcher that dies on the first network blip is not a watcher.
- **Every cycle is audited.** Each cycle appends an `audit` entry (`action="watch.cycle"`) naming
  the adapters run, what was fetched, ingested, deduplicated, and how many findings resulted. An
  unattended process that writes nothing to the audit trail is exactly the thing NYX exists to
  catch.
- **Adapter construction is extracted from `doctor` into a shared factory.** Today
  `src/nyx/cli/main.py` is the only place that builds an `AdapterRegistry`. A second copy in the
  watcher would be a second source of truth that drifts; the watcher and `doctor` must construct
  the identical set from the identical settings.
- **Off by default, configured by the same `NYX_*` environment as everything else.**
  `NYX_WATCH_ENABLED`, `NYX_WATCH_INTERVAL_SECONDS`, `NYX_WATCH_REF`. No adapter is enabled and no
  interval is set, so the command fails closed with a named-variable message rather than
  watching nothing and reporting success.

## Consequences

- **The Git/CI adapter finally has a consumer.** Phase 5's exit criterion — adapters can be
  disabled independently and core NYX still works — becomes observable in production rather than
  only in a test.
- **The watcher cannot be mistaken for a judge.** It produces findings and stops. Closing them
  stays a human act, audited, exactly as SPEC §6 requires.
- **A supervisor is still required.** `nyx watch` does not daemonise itself; the operator runs it
  under whatever keeps processes alive on their host. That is deliberate: process supervision is
  a host concern, and a service that re-implements it will do it worse.
- **The cycle audit entry is new surface.** `nyx audit verify` will verify it like any other
  entry, so a tampered or missing cycle is detectable within the implemented threat model.

## Reversibility

Every decision is additive and reversible. Removing `src/nyx/watch/` and the `watch` command
leaves the rest of NYX untouched; no migration is required because the watcher writes no new
record type.
