# Acceptance harness

An **independent** check that NYX satisfies its acceptance criteria from *outside* the application.

## Why it exists

The project's own `pytest` suite is written by the same agent that writes the code, so a single bad
shared helper can hide the same bug everywhere. This harness deliberately shares nothing with it:

- it starts a **real uvicorn server** as a subprocess and talks to it over **real HTTP**
- it drives the **real CLI** as subprocesses
- it reads the **SQLite file directly** to count rows and scan for secrets, rather than trusting what
  the API reports
- it imports **no `nyx.*` module** and uses **no `TestClient`**

If the service lies about its state, this harness catches it. If the project's test helpers are
wrong, this harness is unaffected.

## Run it

```bash
uv run python scripts/acceptance_harness.py
```

It creates a fresh temporary database, applies migrations, starts the server on port **8097**
(override with `NYX_HARNESS_PORT`), runs every check, prints a summary, then always tears the server
down and removes the temp DB — including on failure and on Ctrl-C.

Exit code is **non-zero if any check FAILs**. All-PASS or PASS-with-SKIP exits zero.

## Verdicts

| Verdict | Meaning |
|---|---|
| `PASS` | The scenario was exercised end to end and held. |
| `FAIL` | The scenario was exercised and did **not** hold. This is a real finding — do not weaken the check to silence it. |
| `SKIP` | The scenario needs a feature that does not exist yet, named in the detail column. Never counted as a pass. |

## Coverage today

Implemented and exercised (Phase 1 surface):

- **Scenario 1** — ingest an event, retrieve it by id, trace it to its source system
- **Scenario 2** — identical replay is idempotent; a different producer id with byte-identical
  content creates a *new* record (identity dedupe, never content-similarity)
- **Scenario 3** — invalid schema is rejected with a structured error and creates **no** row
  (verified by counting rows in SQLite directly)
- **Scenario 14** — a secret in the inbound payload is absent from the persisted row **and** absent
  from the raw database file
- **Scenario 18** — `/api/v1/ready` reports not-ready when migrations are behind head, and ingestion
  fails safely rather than reporting success

Plus CLI checks: `nyx doctor` exit code, `system register`/`list` round-trip, `ingest event`, and a
deliberately invalid input exiting non-zero.

Everything else (findings, critics, rebuttals, verification, dispositions, adapters, auth) reports
`SKIP` with the missing feature named. As later phases land, replace the SKIP with a real check —
never with a fake pass.

## Relationship to `probe_live_api.py`

`scripts/probe_live_api.py` is the smaller, earlier probe this harness grew out of. It is still
useful for a quick single-purpose check. The harness is the generalised version and is what the
delivery report should cite.
