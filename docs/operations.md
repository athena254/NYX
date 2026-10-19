# NYX operations — local Phase 1

**Last updated:** 19 October 2026.

This documents the Phase 1 local/controlled deployment surface. NYX is not yet hardened for
untrusted public networks: authentication, authorisation, and rate limits are Phase 4.

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
| `NYX_AUTH_MODE` | `none` | Phase 1 local mode. |
| `NYX_LLM_ENABLED` | `false` | No model calls in Phase 1. |

The effective configuration is rendered with secret values redacted; do not add secrets to
`NYX_*` values that are echoed by diagnostics.

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

Ingestion is atomic per item. A batch is not atomic as a whole: accepted items remain committed
while a rejected item is reported individually.

## Deferred operational work

Authentication/authorisation, rate limiting, retention jobs, external adapters, finding/verification
workflows, and container/CI hardening are later phases. There is no automated backup scheduler or
metrics endpoint yet; monitor logs and `/ready` externally.
