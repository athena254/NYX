# NYX HTTP API

**Source:** `docs/SPEC.md` §9.1, §5.7. **Last updated:** 20 October 2026.

The generated, authoritative document is checked in at `docs/openapi.json` and served at
`/openapi.json` with interactive docs at `/docs`. This page is a human-readable summary; if it
disagrees with `docs/openapi.json`, the generated document wins.

All routes are under `/api/v1`. Every response carries an `X-Request-Id` header, echoed as
`request_id` in structured errors. Errors use the envelope defined in
`docs/api-error-conventions.md`.

## Endpoints

| Method | Path | Purpose | Success |
| --- | --- | --- | --- |
| `GET` | `/api/v1/health` | Liveness. | `200` |
| `GET` | `/api/v1/ready` | Readiness: database reachable and migrations at head. | `200` / `503` |
| `POST` | `/api/v1/systems` | Register a monitored system. Idempotent on name + environment. | `201` created / `200` existing |
| `GET` | `/api/v1/systems` | List systems (cursor pagination). | `200` |
| `POST` | `/api/v1/events` | Ingest one normalized event. | `201` |
| `GET` | `/api/v1/events/{event_id}` | Retrieve one event. | `200` / `404` |
| `POST` | `/api/v1/events/batch` | Ingest a bounded batch, per-item outcomes. | `200` |
| `POST` | `/api/v1/claims` | Register a scoped claim. | `201` |
| `GET` | `/api/v1/claims/{claim_id}` | Retrieve one claim. | `200` / `404` |
| `POST` | `/api/v1/evidence` | Register an evidence item or reference. | `201` |
| `GET` | `/api/v1/evidence/{evidence_id}` | Retrieve one evidence item. | `200` / `404` |
| `GET` | `/api/v1/critics` | List the registered critics and their declared scope. | `200` |
| `POST` | `/api/v1/critics/run` | Run the selected critics against one event. | `200` |
| `GET` | `/api/v1/findings` | List findings (filtered, cursor-paginated). | `200` |
| `GET` | `/api/v1/findings/{finding_id}` | Retrieve one finding with its evidence links. | `200` / `404` |

### Dispute protocol (Phase 3)

| Method | Path | Purpose | Success |
| --- | --- | --- | --- |
| `POST` | `/api/v1/findings/{finding_id}/rebuttals` | Challenge a finding. Appends; never overwrites. | `200` |
| `GET` | `/api/v1/findings/{finding_id}/rebuttals` | List a finding's rebuttals, oldest first. | `200` |
| `POST` | `/api/v1/findings/{finding_id}/verification-jobs` | Queue an allowlisted verification check. | `200` |
| `GET` | `/api/v1/findings/{finding_id}/verification-jobs` | List a finding's verification attempts. | `200` |
| `POST` | `/api/v1/findings/{finding_id}/verification-jobs/run` | Run the finding's queued jobs. | `200` |
| `GET` | `/api/v1/verification-jobs/{job_id}` | Retrieve one verification attempt. | `200` / `404` |
| `POST` | `/api/v1/findings/{finding_id}/dispositions` | Record an authorised outcome. | `200` |
| `GET` | `/api/v1/findings/{finding_id}/history` | The full dispute record, including the chain. | `200` |
| `GET` | `/api/v1/policies/versions` | Recorded policy changes, newest first. | `200` |

There is **no `PATCH` and no `DELETE`** in the dispute surface. Nothing in the protocol rewrites a
prior record: a disposition supersedes by linking, a rebuttal appends, and a verification attempt is a
new row. See `docs/dispute-protocol.md` for the loop end to end.

Two failure codes are specific to this surface:

- `UNKNOWN_EVIDENCE_ID` (`422`) — a rebuttal cited evidence that does not exist. Refused before
  anything is stored.
- `VERIFICATION_NOT_PERMITTED` (`403`) — the requested check is not on the allowlist. Refused before
  anything executes, so untrusted input never reaches a command line.
- `CAPABILITY_REQUIRED` (`403`) — the caller may not change a disposition. The checker defaults to
  deny-all until Phase 4 wires authorisation.

## Request bodies

Bodies are validated against the producer-facing input models in `src/nyx/ingestion/inputs.py`.
They mirror the record schemas in `schemas/*.schema.json` with two deliberate differences:

- server-assigned fields (`id`, `received_at`, `content_hash`, `created_at`) are optional and
  ignored when supplied, so a previously exported example can be replayed;
- an optional `producer_event_id` supplies the producer's own identity for deduplication
  (SPEC §11.2). It is a transport field and is not part of the checked-in JSON Schemas.

Unknown fields are rejected (`SCHEMA_VALIDATION_FAILED`, `422`).

### Examples

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/systems \
  -H 'Content-Type: application/json' \
  --data @examples/system-manifest.json

curl -sS -X POST http://127.0.0.1:8000/api/v1/events \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: run-42-event-1' \
  --data @tests/fixtures/event_completion_claim.json
```

## Idempotency

Deduplication is **identity-based only** — a `producer_event_id` and/or an `Idempotency-Key`
request header. Content similarity is never used to merge records, and there is no code path to do
so. Behaviour:

- an exact replay returns `409 DUPLICATE_EVENT` naming the original record ID; no second row is
  created;
- reusing a key with a different payload returns `409 IDEMPOTENCY_KEY_REUSED`;
- a distinct `producer_event_id` with byte-identical content still creates a second record.

Limitation: deduplication scope is per record type, not per source system, so producers must keep
`producer_event_id` values unique within a record type.

## Batch

`POST /api/v1/events/batch` accepts `{"events": [ ... ]}` (a bare JSON array is accepted by the
CLI). Each item is validated and persisted in its own transaction:

- one invalid item is reported as `rejected` in `results[]` and does not fail the others;
- the response `request_id` correlates the whole batch;
- a batch larger than `NYX_MAX_BATCH_SIZE` fails the whole request with `BATCH_TOO_LARGE` (`413`).

## Pagination

List endpoints are deterministically ordered and cursor-paginated. Pass `limit` (clamped) and the
opaque `next_cursor` from the previous page as `cursor`. An unparseable cursor is
`400 INVALID_REQUEST`.

## Redaction

Incoming payloads are redacted **before persistence and before any model call** (SPEC §12). The
redaction runs across the whole validated record; matched values are replaced with an opaque
placeholder and the rule names and JSON paths are recorded in the audit rationale. Values are never
logged or echoed. If redaction cannot complete safely the write is refused with
`500 REDACTION_FAILED` and nothing is persisted. See `docs/privacy-and-retention.md`.

## Deferred

Authentication, authorisation, rate limits, retention jobs, and audit-chain verification are later
phases. The dispute protocol records the actor from the request body today, so every dispute record
is self-asserted until Phase 4 replaces it with a verified principal; the audit trail states this.

Until then the API is intended for local or trusted-network use only.
