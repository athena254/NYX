# NYX API error conventions

**Status:** Phase 0 contract. Phase 1 implements the handlers and tests that enforce it.
**Applies to:** all `/api/v1` endpoints, the CLI in `--json` mode, batch per-item results, and `nyx
doctor` structured output.

## 1. Principles

1. **Machine-readable first.** Every failure carries a stable `code` that clients can branch on. Codes
   are part of the contract and change only with a documented approval.
2. **Human-readable always.** Every error carries a plain `message` that is safe to show to an
   operator and contains no secret, token, raw payload, or restricted evidence.
3. **Traceable.** Every error carries a `request_id`. The same ID appears in the service log line for
   that request so an operator can correlate without searching payloads.
4. **Precise when possible.** Validation errors include `field_details` naming the JSON pointer and
   the reason. Errors that are not about a specific field omit it.
5. **No partial writes.** A rejected mutation leaves stored state unchanged. Batch operations report
   per-item outcomes.
6. **No leaks.** Error messages and field details never echo secrets, credentials, restricted
   evidence, or full claim text. A redaction failure fails closed.

## 2. Error envelope

Content type: `application/json`. The HTTP status code carries the class of failure; the body carries
the machine code and detail. The envelope has no other top-level keys.

```json
{
  "code": "SCHEMA_VALIDATION_FAILED",
  "message": "Request body failed schema validation.",
  "request_id": "req_01JBG7Z4W9V3QK8M2E5T6Y7A1B",
  "field_details": [
    {
      "field": "/occurred_at",
      "issue": "must be an RFC 3339 UTC timestamp",
      "code": "invalid_format"
    }
  ]
}
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `code` | string, `^[A-Z][A-Z0-9_]*$` | yes | Stable machine code from the catalogue below. |
| `message` | string, 1–500 chars | yes | Safe human explanation. Never a stack trace or secret. |
| `request_id` | string | yes | Server-generated correlation ID, also present in logs and the `X-Request-Id` response header. If the caller supplied a valid `X-Request-Id`, it is echoed; otherwise the server generates one. |
| `field_details` | array of objects | no | Present for field-level failures. Each item has `field` (JSON pointer), `issue`, and an optional `code`. |

`field_details` item shape:

```json
{
  "field": "/evidence/0/source_uri",
  "issue": "scheme is not allowed; only http and https are permitted",
  "code": "invalid_scheme"
}
```

## 3. HTTP status usage

| Status | Used for |
| --- | --- |
| `400 Bad Request` | Malformed request that is not a schema-validation result (for example an unparseable body). |
| `401 Unauthorized` | Missing or invalid credentials. |
| `403 Forbidden` | Authenticated caller lacks the required capability. |
| `404 Not Found` | The referenced resource does not exist or is not visible to the caller. |
| `409 Conflict` | Idempotency or state conflict (duplicate, version mismatch, invalid transition). |
| `413 Payload Too Large` | Body or batch exceeds the configured limit. |
| `422 Unprocessable Entity` | Body parsed but failed schema or semantic validation. |
| `429 Too Many Requests` | Rate limit exceeded; includes `Retry-After`. |
| `500 Internal Server Error` | Unexpected failure; message is generic. |
| `503 Service Unavailable` | A required dependency (database, enabled adapter) is unavailable. |

`401` and `403` do not reveal whether a hidden resource exists. A restricted evidence item the caller
cannot read returns `404` or an omitted field, not `403` with its identifier in the message.

## 4. Batch outcomes

`POST /api/v1/events/batch` returns `200` with a per-item result. Each item reuses the same error
envelope:

```json
{
  "results": [
    {"index": 0, "status": "created", "record_id": "evt_01JBG7..."},
    {
      "index": 1,
      "status": "rejected",
      "error": {
        "code": "SCHEMA_VALIDATION_FAILED",
        "message": "Event failed schema validation.",
        "request_id": "req_01JBG7Z4W9V3QK8M2E5T6Y7A1B",
        "field_details": [
          {"field": "/source_system_id", "issue": "unknown system", "code": "not_found"}
        ]
      }
    }
  ],
  "request_id": "req_01JBG7Z4W9V3QK8M2E5T6Y7A1B"
}
```

A batch is not all-or-nothing. An item-level failure never rolls back accepted items in the same
request unless the endpoint documents transactional batching.

## 5. Initial error-code catalogue

`retryable` means the same request may succeed later without changing the payload.

| Code | HTTP | Retryable | Meaning |
| --- | --- | --- | --- |
| `INVALID_REQUEST` | 400 | no | The request could not be parsed or is structurally malformed. |
| `SCHEMA_VALIDATION_FAILED` | 422 | no | The body parsed but failed schema validation. `field_details` lists failures. |
| `UNSUPPORTED_SCHEMA_VERSION` | 422 | no | The record's `schema_version` is not supported by this service. |
| `UNSCOPED_CLAIM` | 422 | no | A claim is missing scope required to assess it. |
| `UNKNOWN_EVIDENCE_ID` | 422 | no | A referenced evidence ID does not exist or is not visible. |
| `PAYLOAD_TOO_LARGE` | 413 | no | A single payload exceeds the configured maximum. |
| `BATCH_TOO_LARGE` | 413 | no | The batch exceed the configured maximum item count. |
| `UNAUTHENTICATED` | 401 | no | No valid credentials were presented. |
| `FORBIDDEN` | 403 | no | The caller is authenticated but lacks the capability for this action. |
| `CAPABILITY_REQUIRED` | 403 | no | A specific named capability is required; the code is included in the message. |
| `EVIDENCE_ACCESS_DENIED` | 403 | no | The caller cannot read the requested evidence at its sensitivity level. |
| `NOT_FOUND` | 404 | no | The resource does not exist or is not visible to the caller. |
| `CONFLICT` | 409 | no | The request conflicts with current state. |
| `DUPLICATE_EVENT` | 409 | no | The producer event ID or idempotency key was already accepted. The existing record ID is included. |
| `IDEMPOTENCY_KEY_REUSED` | 409 | no | The idempotency key was reused with a different payload. |
| `INVALID_LIFECYCLE_TRANSITION` | 409 | no | The requested finding transition is not allowed from its current state. |
| `POLICY_NOT_FOUND` | 409 | no | The referenced policy or version does not exist. |
| `POLICY_VERSION_MISMATCH` | 409 | no | The supplied policy version does not match the active decision version. |
| `EVIDENCE_INTEGRITY_FAILED` | 409 | no | Stored content does not match its recorded digest. |
| `STALE_EVIDENCE` | 409 | no | The evidence is outside its validity window for this action. |
| `VERIFICATION_NOT_PERMITTED` | 403 | no | The requested verification is not on the allowlist for this adapter or caller. |
| `VERIFICATION_FAILED` | 500 | yes | The verification tool ran and failed to produce a usable result. Details are redacted. |
| `RATE_LIMITED` | 429 | yes | The caller exceeded the configured rate limit. |
| `DEPENDENCY_UNAVAILABLE` | 503 | yes | A required dependency is unavailable. |
| `STORAGE_UNAVAILABLE` | 503 | yes | The database is unavailable or not writable. |
| `DATABASE_NOT_READY` | 503 | yes | The database is reachable but not ready, for example migrations are behind head. |
| `INTERNAL_ERROR` | 500 | yes | An unexpected internal error occurred. The message is generic. |
| `REDACTION_FAILED` | 500 | no | A payload could not be safely redacted; the write was refused and nothing was persisted. |

`INTERNAL_ERROR` and `REDACTION_FAILED` never include the offending value. `EVIDENCE_INTEGRITY_FAILED`
names the evidence ID and the expected/observed digest prefixes but never the content.

## 6. Validation field codes

`field_details[].code` is a lower-snake token, distinct from the top-level error code. Initial values:

| Field code | Meaning |
| --- | --- |
| `required` | A required field is missing. |
| `invalid_type` | The value is the wrong JSON type. |
| `invalid_format` | The value does not match the declared format (date-time, digest, URI). |
| `invalid_enum` | The value is not one of the allowed values. |
| `too_short` / `too_long` | Length constraint violated. |
| `out_of_range` | Numeric or date range violated. |
| `not_found` | A referenced ID does not exist or is not visible. |
| `forbidden_value` | The value is structurally valid but not permitted (for example a private-network URL). |
| `duplicate` | A uniqueness constraint is violated. |
| `unknown_field` | A disallowed additional field was present. |

## 7. CLI and `nyx doctor`

In `--json` mode the CLI writes the same envelope to standard output and exits non-zero. Human mode
prints `code`, `message`, and `request_id` (when present) to standard error. `nyx doctor` reports each
check with a `status` of `ok`, `warn`, or `fail`, a `code` from this catalogue when applicable, and a
safe message; it never prints credentials or connection strings with passwords.

## 8. Security requirements for errors

- Never include `Authorization` headers, cookies, tokens, private keys, or raw secrets in `message`,
  `field_details`, logs, or `request_id`.
- Do not echo a restricted evidence payload in an error, even when it caused the failure.
- A malformed request must not produce a partial write.
- Authorization failures must not disclose the existence of resources the caller cannot access.
- Error handling must not execute code paths derived from the failing payload.
- Every `5xx` is logged once with `request_id` and without request-body secrets.
