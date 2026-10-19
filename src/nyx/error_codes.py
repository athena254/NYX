"""Machine-readable error-code catalogue shared by ingestion, API, and CLI.

Every failure in NYX carries a stable ``code``. The code is part of the public contract
(``docs/api-error-conventions.md``), so codes are added deliberately and never renamed
silently. This module is intentionally dependency-free so that ingestion, the HTTP API, and
the CLI can all agree on the same vocabulary without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    """The stable set of NYX error codes."""

    INVALID_REQUEST = "INVALID_REQUEST"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    UNSCOPED_CLAIM = "UNSCOPED_CLAIM"
    UNKNOWN_EVIDENCE_ID = "UNKNOWN_EVIDENCE_ID"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    BATCH_TOO_LARGE = "BATCH_TOO_LARGE"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    CAPABILITY_REQUIRED = "CAPABILITY_REQUIRED"
    EVIDENCE_ACCESS_DENIED = "EVIDENCE_ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    INVALID_LIFECYCLE_TRANSITION = "INVALID_LIFECYCLE_TRANSITION"
    POLICY_NOT_FOUND = "POLICY_NOT_FOUND"
    POLICY_VERSION_MISMATCH = "POLICY_VERSION_MISMATCH"
    EVIDENCE_INTEGRITY_FAILED = "EVIDENCE_INTEGRITY_FAILED"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    VERIFICATION_NOT_PERMITTED = "VERIFICATION_NOT_PERMITTED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    DATABASE_NOT_READY = "DATABASE_NOT_READY"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    REDACTION_FAILED = "REDACTION_FAILED"


@dataclass(frozen=True)
class ErrorSpec:
    """The transport and retry metadata for an :class:`ErrorCode`."""

    http_status: int
    retryable: bool
    default_message: str


_CATALOGUE: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.INVALID_REQUEST: ErrorSpec(
        400, False, "The request could not be parsed or is structurally malformed."
    ),
    ErrorCode.SCHEMA_VALIDATION_FAILED: ErrorSpec(
        422, False, "Request body failed schema validation."
    ),
    ErrorCode.UNSUPPORTED_SCHEMA_VERSION: ErrorSpec(
        422, False, "The record's schema_version is not supported by this service."
    ),
    ErrorCode.UNSCOPED_CLAIM: ErrorSpec(
        422, False, "A claim is missing scope required to assess it."
    ),
    ErrorCode.UNKNOWN_EVIDENCE_ID: ErrorSpec(
        422, False, "A referenced evidence ID does not exist or is not visible."
    ),
    ErrorCode.PAYLOAD_TOO_LARGE: ErrorSpec(
        413, False, "A single payload exceeds the configured maximum."
    ),
    ErrorCode.BATCH_TOO_LARGE: ErrorSpec(
        413, False, "The batch exceeds the configured maximum item count."
    ),
    ErrorCode.UNAUTHENTICATED: ErrorSpec(401, False, "No valid credentials were presented."),
    ErrorCode.FORBIDDEN: ErrorSpec(
        403, False, "The caller is authenticated but lacks the capability for this action."
    ),
    ErrorCode.CAPABILITY_REQUIRED: ErrorSpec(
        403, False, "A specific named capability is required for this action."
    ),
    ErrorCode.EVIDENCE_ACCESS_DENIED: ErrorSpec(
        403, False, "The caller cannot read the requested evidence at its sensitivity level."
    ),
    ErrorCode.NOT_FOUND: ErrorSpec(
        404, False, "The resource does not exist or is not visible to the caller."
    ),
    ErrorCode.CONFLICT: ErrorSpec(409, False, "The request conflicts with current state."),
    ErrorCode.DUPLICATE_EVENT: ErrorSpec(
        409, False, "The producer event ID or idempotency key was already accepted."
    ),
    ErrorCode.IDEMPOTENCY_KEY_REUSED: ErrorSpec(
        409, False, "The idempotency key was reused with a different payload."
    ),
    ErrorCode.INVALID_LIFECYCLE_TRANSITION: ErrorSpec(
        409, False, "The requested finding transition is not allowed from its current state."
    ),
    ErrorCode.POLICY_NOT_FOUND: ErrorSpec(
        409, False, "The referenced policy or version does not exist."
    ),
    ErrorCode.POLICY_VERSION_MISMATCH: ErrorSpec(
        409, False, "The supplied policy version does not match the active decision version."
    ),
    ErrorCode.EVIDENCE_INTEGRITY_FAILED: ErrorSpec(
        409, False, "Stored content does not match its recorded digest."
    ),
    ErrorCode.STALE_EVIDENCE: ErrorSpec(
        409, False, "The evidence is outside its validity window for this action."
    ),
    ErrorCode.VERIFICATION_NOT_PERMITTED: ErrorSpec(
        403, False, "The requested verification is not on the allowlist for this adapter or caller."
    ),
    ErrorCode.VERIFICATION_FAILED: ErrorSpec(
        500, True, "The verification tool ran and failed to produce a usable result."
    ),
    ErrorCode.RATE_LIMITED: ErrorSpec(429, True, "The caller exceeded the configured rate limit."),
    ErrorCode.DEPENDENCY_UNAVAILABLE: ErrorSpec(503, True, "A required dependency is unavailable."),
    ErrorCode.STORAGE_UNAVAILABLE: ErrorSpec(
        503, True, "The database is unavailable or not writable."
    ),
    ErrorCode.DATABASE_NOT_READY: ErrorSpec(
        503,
        True,
        "The database is reachable but not ready (for example, migrations are behind head).",
    ),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, True, "An unexpected internal error occurred."),
    ErrorCode.REDACTION_FAILED: ErrorSpec(
        500, False, "A payload could not be safely redacted; the write was refused."
    ),
}


def spec_for(code: ErrorCode) -> ErrorSpec:
    """Return the catalogue entry for ``code``."""

    return _CATALOGUE[code]


def http_status_for(code: ErrorCode) -> int:
    """Return the HTTP status associated with ``code``."""

    return _CATALOGUE[code].http_status


def default_message_for(code: ErrorCode) -> str:
    """Return the safe default message associated with ``code``."""

    return _CATALOGUE[code].default_message
