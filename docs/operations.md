# NYX operations — local service

**Last updated:** 19 October 2026.

This documents the local/controlled deployment surface. Phase 4 adds authentication,
authorisation, and rate limiting; their operational limits are recorded here rather than implying
more safety than the implementation provides.

## Prerequisites

- Python 3.12+ (the development venv is 3.14) and [`uv`](https://docs.astral.sh/uv/).
- SQLite is the local default. The schema is migration-managed; do not create tables by hand and do
  not rely on `create_all` outside tests.

```bash
uv sync
uv run alembic upgrade head
uv run nyx doctor
```

`nyx doctor` checks: effective configuration (secret-safe), data-directory writability, database
reachability, Alembic revision vs head, optional integrations being disabled, API importability, and
whether the checked-in `docs/openapi.json` is stale. It exits non-zero if any check fails and never
prints secret values.

## Running the service

```bash
uv run uvicorn nyx.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

Endpoints: `/api/v1/health` (liveness), `/api/v1/ready` (readiness), `/api/v1/systems`,
`/api/v1/events`, `/api/v1/claims`, `/api/v1/evidence`. See `docs/api.md`.

Readiness semantics:

- database unreachable → `503 STORAGE_UNAVAILABLE`;
- database reachable but Alembic is behind head (or has no revision) → `503 DATABASE_NOT_READY`;
- both checks pass → `200 {"status": "ready", ...}`.

A failed readiness check never reports successful ingestion.

## Running the watcher

`nyx watch` observes the enabled adapters on a schedule and records what it finds. It is the
component that turns NYX from a tool you drive into a service that watches things for you. It is
**off by default** and must be switched on explicitly. Design and constraints are in
[`docs/adr/0009-unattended-operation.md`](adr/0009-unattended-operation.md).

What a cycle does, for each enabled adapter: probe its health, fetch the configured ref, push the
adapter's findings through the ordinary ingestion gateway (so they inherit redaction, schema
validation, and deduplication for free), run the deterministic critic pack, and append one
`watch.cycle` audit entry. It is **read-only**: it never files a rebuttal, never records a
disposition, and never changes a lifecycle state. A finding it produces is an ordinary
`detected` finding that a human still closes.

Enable it:

```bash
# .env
NYX_WATCH_ENABLED=true
NYX_WATCH_REF=main            # the git ref to observe; required, never empty
NYX_WATCH_INTERVAL_SECONDS=300  # between cycles; 0 < x <= 86400
# plus the adapter config the watcher shares with the rest of NYX (e.g. NYX_ENABLED_ADAPTERS,
# NYX_GIT_CI_REPOSITORY, ...) -- see the Configuration section above.
```

Run it once (for cron, or to check it works):

```bash
uv run python -m nyx.cli.main watch --once
```

Run it continuously (under a supervisor — NYX does not daemonise itself):

```bash
uv run python -m nyx.cli.main watch
```

Two properties that make a scheduled watcher safe:

- **A cycle is idempotent.** Fetching the same unchanged ref twice creates no new rows the second
  time — ingestion deduplicates on identity. A supervisor may re-run `--once` freely.
- **One bad cycle never kills the loop.** An adapter that is down is recorded as a `degraded`
  outcome with the reason in its detail, and the loop continues. `nyx watch` never exits on a
  network blip; process supervision (systemd, a cron loop, a container restart policy) is what
  keeps it alive, and is a host concern NYX deliberately does not re-implement.

Fail-closed: with `NYX_WATCH_ENABLED` unset or false, `nyx watch` refuses with a named-variable
message and a non-zero exit — it never watches nothing and reports success. `nyx doctor` reports
the watcher's configuration (enabled, interval, ref) but does not probe adapter reachability
there; that is checked at run time, inside each cycle.

## API rate limiting

The HTTP API applies a per-caller sliding-window rate limit, enabled by default. Callers are keyed
by authenticated principal id when one is present and by client host otherwise; a caller-supplied
header such as `X-Forwarded-For` is never used as the key. `/api/v1/health` and `/api/v1/ready` are
exempt, so a saturated API can still report liveness and readiness. A refused request returns
`429 RATE_LIMITED` with a `Retry-After` header, through the same error envelope as other failures.

**Non-claim:** the limiter is in-memory and per-process. Each API process keeps its own counters, so
a deployment with multiple workers or replicas enforces the limit per process, not cluster-wide: N
processes can allow up to N times the configured budget. Cluster-wide limiting requires a shared
store (for example Redis) and is not implemented.

## Configuration

Configuration is environment-first with the `NYX_` prefix (`src/nyx/config.py`). Local defaults bind
to loopback and a file-backed SQLite database. Relevant operational settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `NYX_DATABASE_URL` | local SQLite file | SQLAlchemy URL. |
| `NYX_DATA_DIR` | local data directory | Writable root for local state. |
| `NYX_MAX_PAYLOAD_BYTES` | `1048576` | Maximum canonical size of one record. |
| `NYX_MAX_BATCH_SIZE` | `100` | Maximum items in one batch request. |
| `NYX_REDACTION_ENABLED` | `true` | Do not disable outside a controlled test. |
| `NYX_LOG_LEVEL` | `INFO` | Structured JSON logs. |
| `NYX_AUTH_MODE` | `none` | Local loopback mode; `api_key` requires credentials. |
| `NYX_RATE_LIMIT_ENABLED` | `true` | Apply the per-caller API rate limit. |
| `NYX_RATE_LIMIT_REQUESTS` | `120` | Allowed requests per window, per caller. |
| `NYX_RATE_LIMIT_WINDOW_SECONDS` | `60.0` | Sliding-window length. |
| `NYX_RATE_LIMIT_EXEMPT_PATHS` | health, ready | Paths never rate limited. |
| `NYX_LLM_ENABLED` | `false` | No conversational model calls by default. |
| `NYX_LLM_CRITIC_ENABLED` | `false` | Register the c011 model critic; requires `NYX_LLM_MODEL`. |
| `NYX_LLM_LOG_PROMPTS` | `false` | Store the redacted prompt/response pair on each run; sensitive. |
| `NYX_LLM_MODEL` / `NYX_LLM_PROTOCOL` / `NYX_LLM_BASE_URL` | unset / `openai` / dmr-X | Shared by the chat shell and the model critic. |

The effective configuration is rendered with secret values redacted; do not add secrets to
`NYX_*` values that are echoed by diagnostics.

### Model critic

`NYX_LLM_CRITIC_ENABLED=true` registers c011 alongside the deterministic pack; it is absent by
default, and enabling it without `NYX_LLM_MODEL` refuses at startup. Every run persists the
provider, model, prompt-template version, and token usage; the prompt and raw response are stored
only when `NYX_LLM_LOG_PROMPTS=true`, and are redacted at the persistence boundary. A model may
propose a finding, but it never records a disposition or changes a lifecycle state. Routing the
prompt body to a provider is a deployment privacy decision; see `docs/privacy-and-retention.md`.
The evaluated path and its current limits are in `docs/implementation/phase-6-evaluation.md`;
no live provider calibration run has been recorded yet.

## Logs and request correlation

Logs are single-line JSON. Each request gets an `X-Request-Id` (client-supplied or generated) that
is bound to the log context and returned in the response and structured errors. Log messages never
include payload values, credentials, or raw request bodies. Redaction findings are reported by rule
name and JSON path only.

## Backups and migrations

- Back up the SQLite file (and `NYX_DATA_DIR`) before upgrading.
- Apply migrations with `uv run alembic upgrade head`; roll back one revision with
  `uv run alembic downgrade -1`.
- The audit chain detects accidental or partial edits within its documented threat model; it does
  not defend against an administrator who rewrites the whole database (see `docs/threat-model.md`).

## Failure handling

| Failure | Behaviour |
| --- | --- |
| Database unavailable | `503 STORAGE_UNAVAILABLE`; nothing is written. |
| Migrations behind head | `/ready` → `503 DATABASE_NOT_READY`; keyed ingestion fails rather than writing into an old schema. |
| Payload exceeds the limit | `413 PAYLOAD_TOO_LARGE` before parsing. |
| Batch exceeds the limit | `413 BATCH_TOO_LARGE`; no items are processed. |
| Schema invalid | `422 SCHEMA_VALIDATION_FAILED` with `field_details`; no partial write. |
| Duplicate identity | `409 DUPLICATE_EVENT` naming the original record; no second row. |
| Redaction cannot complete | `500 REDACTION_FAILED`; nothing is persisted. |
| Rebuttal cites unknown evidence | `422 UNKNOWN_EVIDENCE_ID`; nothing is stored. |
| Check not on the allowlist | `403 VERIFICATION_NOT_PERMITTED`; nothing is executed. |
| Disposition without the capability | `403 CAPABILITY_REQUIRED`; the finding is unchanged. |
| Invalid lifecycle transition | `409 INVALID_LIFECYCLE_TRANSITION`; the stored state is unchanged. |

Ingestion is atomic per item. A batch is not atomic as a whole: accepted items remain committed
while a rejected item is reported individually.

## Disputing a finding

The dispute protocol is described end to end in `docs/dispute-protocol.md`. Operationally:

```bash
# Challenge a finding; the rebuttal appends and the finding is not overwritten.
curl -sS -X POST http://127.0.0.1:8000/api/v1/findings/fnd_xxx/rebuttals \
  -H 'Content-Type: application/json' \
  -d '{"submitted_by": "sys_ghost_factory_demo",
       "argument": "The attached report covers commit abc123.",
       "evidence_ids": ["ev_xxx"],
       "requested_outcome": "withdrawn"}'

# Re-examine it. A check that is not allowlisted is refused before anything runs.
curl -sS -X POST http://127.0.0.1:8000/api/v1/findings/fnd_xxx/verification-jobs \
  -d '{"check_id": "critics.rerun-linked", "requested_by": "principal_operator"}'
curl -sS -X POST http://127.0.0.1:8000/api/v1/findings/fnd_xxx/verification-jobs/run

# Read everything recorded, in the order it happened.
curl -sS http://127.0.0.1:8000/api/v1/findings/fnd_xxx/history
```

The CLI equivalents are `nyx finding rebut`, `nyx finding verify`, `nyx finding dispose`, and
`nyx finding history`.

Three operational facts worth knowing:

- **A default deployment refuses dispositions.** The capability checker is deny-all until Phase 4
  wires authorisation, so `nyx finding dispose` exits non-zero with `CAPABILITY_REQUIRED` by design.
  Rebuttals and verification requests still work.
- **Nothing is ever deleted.** Rebuttals, verification attempts, dispositions, and policy versions
  are append-only. A new disposition supersedes the previous one by linking to it; the earlier row
  stays readable forever. There is no `PATCH` or `DELETE`.
- **A verification attempt is not a verdict.** It records whether the finding reproduces, together
  with an explicit independence statement. Only an authorised disposition moves the finding.

## Container deployment

`Dockerfile` builds a minimal image: dependencies from the lockfile, the service as a non-root
user, migrations run at container start.

```bash
docker build -t nyx .
docker run --rm -p 127.0.0.1:8000:8000 \
  -e NYX_DATABASE_URL=sqlite:///./data/nyx.sqlite3 \
  -e NYX_DATA_DIR=/app/data \
  -v nyx-data:/app/data \
  nyx
```

The image binds **loopback by default**. Exposing it requires both of these, and `Settings`
refuses the unsafe combination rather than starting:

- `NYX_HOST=0.0.0.0` — a non-loopback bind
- `NYX_AUTH_MODE=api_key` (or `local`) with a credential configured

A container started with a non-loopback host and no authentication fails to start. That is the
intended behaviour, not a bug.

No secret is baked into the image. Credentials come from the environment at run time; write them
into a secrets store or a mounted file, never into the image or the build context. `.dockerignore`
excludes `.env`, the virtual environment, the caches, and the local data directory.

## Dependency licensing

NYX is distributed under the Apache License 2.0. A new runtime dependency must be compatible with
that licence; the SPEC §12 review records the candidate's licence, maturity, and maintenance
before it is added, and the default answer is no new dependency. Current runtime dependencies are
MIT, BSD-2-Clause, BSD-3-Clause, or Apache-2.0 (see `docs/implementation/phase-5-plan.md` §1).

## Deferred operational work

- **Rate limiting is per-process**, as described above. A multi-process or multi-host deployment
  needs a shared store; there is no cluster-wide quota yet.
- **No metrics endpoint.** The metric list in SPEC §16 is not implemented; monitor logs and
  `/ready` externally.
- **External adapters are opt-in, not core.** The Git/CI and agent-transcript adapters exist
  (Phase 5) but are constructed only when named in `NYX_ENABLED_ADAPTERS`; `nyx doctor` reports
  registered-versus-enabled. Core NYX works with none enabled.
- **The model critic's evaluation is fixture-only so far.** c011 is wired and its provenance is
  persisted, but no live calibration run or numeric threshold exists yet; see
  `docs/implementation/phase-6-evaluation.md`.
- **Retention purge is opt-in and single-node.** `nyx retention purge` acts on the database it is
  pointed at; there is no scheduler. Run it from a host that can reach the database, and treat
  `--apply` as a change you would not make by accident.
- **There is no metrics endpoint yet.** Monitor logs and `/ready` externally; the metric list in
  SPEC §16 is not implemented.
- **CI is configured but the container image has not been built on this machine** -- Docker is not
  installed here. The workflow builds and scans it in CI.
