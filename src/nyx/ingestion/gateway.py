"""The ordered ingestion pipeline (SPEC §4, §17.2 scenario 3).

Stage order for every record type:

1. size limit (before parsing, so an oversized body cannot exhaust memory);
2. JSON decoding;
3. schema validation against the producer-facing input model;
4. redaction of the whole validated payload (before persistence, before any model call);
5. identity-based deduplication (producer event id and/or idempotency key);
6. persistence **and** audit in one transaction;
7. a typed :class:`IngestResult`.

A failure at any of stages 1–5 returns a rejected result before the session is touched. A
failure at stage 6 rolls the transaction back, so there is never a partial write. The gateway
never owns the session's transaction boundary; callers pass an open ``Session`` and the
service layer wraps it in ``session_scope``.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from nyx.audit.writer import AuditEntryInput, AuditWriter
from nyx.config import Settings
from nyx.domain.base import PrincipalRef, utc_now
from nyx.domain.canonical import content_hash
from nyx.domain.claims import Claim
from nyx.domain.enums import AuditOutcome, PrincipalKind
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem
from nyx.domain.ids import new_claim_id, new_event_id, new_evidence_id, new_system_id
from nyx.domain.systems import MonitoredSystem
from nyx.error_codes import ErrorCode
from nyx.ingestion import idempotency
from nyx.ingestion.errors import (
    FieldDetail,
    IngestResult,
    IngestStatus,
    StructuredError,
    error_for,
    rejected,
)
from nyx.ingestion.inputs import (
    ClaimInput,
    EventInput,
    EvidenceInput,
    SystemManifestInput,
)
from nyx.ingestion.limits import LimitExceededError, enforce_payload_size
from nyx.ingestion.paths import PathTraversalError, is_path_like, resolve_within
from nyx.ingestion.redaction import RedactionError, RedactionReport, redact
from nyx.ingestion.url_fetch import (
    UrlFetchDenied,
    host_resolves,
    is_http_url,
    validate_evidence_url,
)
from nyx.storage.repositories.audit import AuditRepository
from nyx.storage.repositories.claims import ClaimRepository
from nyx.storage.repositories.events import EventRepository
from nyx.storage.repositories.evidence import EvidenceRepository
from nyx.systems.service import register_system as _register_system

SUPPORTED_SCHEMA_VERSIONS = {"1.0"}
_ZERO_DIGEST = "sha256:" + "0" * 64

_ModelT = TypeVar("_ModelT", bound=BaseModel)

_FIELD_CODE_MAP: dict[str, str] = {
    "missing": "required",
    "extra_forbidden": "unknown_field",
    "enum": "invalid_enum",
    "literal_error": "invalid_enum",
    "string_pattern_mismatch": "invalid_format",
    "string_too_short": "too_short",
    "string_too_long": "too_long",
    "list_too_short": "too_short",
    "list_too_long": "too_long",
    "too_short": "too_short",
    "too_long": "too_long",
    "greater_than": "out_of_range",
    "greater_than_equal": "out_of_range",
    "less_than": "out_of_range",
    "less_than_equal": "out_of_range",
    "string_type": "invalid_type",
    "int_type": "invalid_type",
    "bool_type": "invalid_type",
    "dict_type": "invalid_type",
    "list_type": "invalid_type",
    "datetime_type": "invalid_type",
    "datetime_from_date_parsing": "invalid_format",
    "datetime_parsing": "invalid_format",
    "value_error": "invalid_format",
}

_ACTOR = PrincipalRef(
    id="nyx.ingestion-gateway", kind=PrincipalKind.SERVICE, display_name="NYX ingestion gateway"
)


def _field_code(error_type: str) -> str:
    return _FIELD_CODE_MAP.get(error_type, "invalid_format")


def _to_pointer(location: tuple[Any, ...]) -> str:
    if not location:
        return "/"
    parts: list[str] = []
    for token in location:
        text = str(token).replace("~", "~0").replace("/", "~1")
        parts.append(text)
    return "/" + "/".join(parts)


def _field_details(exc: ValidationError) -> list[FieldDetail]:
    details: list[FieldDetail] = []
    for error in exc.errors(include_input=False, include_url=False):
        details.append(
            FieldDetail(
                field=_to_pointer(tuple(error.get("loc", ()))),
                issue=str(error.get("msg", "invalid value")),
                code=_field_code(str(error.get("type", ""))),
            )
        )
    return details


def _has_unscoped_claim(exc: ValidationError) -> bool:
    return any("claim scope must identify" in str(e.get("msg", "")) for e in exc.errors())


class IngestionGateway:
    """Apply the ingestion pipeline for one producer payload at a time."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # -- decoding and validation -------------------------------------------------

    def _decode(self, raw: Any, request_id: str) -> tuple[Any, StructuredError | None]:
        if isinstance(raw, bytes):
            try:
                return json.loads(raw.decode("utf-8")), None
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None, error_for(
                    ErrorCode.INVALID_REQUEST,
                    request_id=request_id,
                    message="Request body is not valid UTF-8 JSON.",
                )
        if isinstance(raw, BaseModel):
            return raw.model_dump(mode="json", by_alias=True), None
        return raw, None

    def _validate(
        self, model: type[_ModelT], value: Any, request_id: str
    ) -> tuple[_ModelT | None, StructuredError | None]:
        try:
            return model.model_validate(value), None
        except ValidationError as exc:
            details = _field_details(exc)
            if model is ClaimInput and _has_unscoped_claim(exc):
                return None, error_for(
                    ErrorCode.UNSCOPED_CLAIM,
                    request_id=request_id,
                    field_details=details,
                )
            return None, error_for(
                ErrorCode.SCHEMA_VALIDATION_FAILED,
                request_id=request_id,
                field_details=details,
            )

    def _redact(
        self, value: Any, request_id: str
    ) -> tuple[Any, RedactionReport, StructuredError | None]:
        if not self._settings.redaction_enabled:
            return value, RedactionReport(enabled=False), None
        try:
            redacted, report = redact(value)
        except RedactionError:
            return (
                None,
                RedactionReport(),
                error_for(ErrorCode.REDACTION_FAILED, request_id=request_id),
            )
        return redacted, report, None

    def _size_check(self, raw: Any, request_id: str) -> StructuredError | None:
        try:
            enforce_payload_size(raw, self._settings)
        except LimitExceededError as exc:
            return error_for(
                ErrorCode.PAYLOAD_TOO_LARGE,
                request_id=request_id,
                message=(
                    f"Payload is {exc.observed} bytes, exceeding the configured maximum "
                    f"of {exc.limit} bytes."
                ),
            )
        return None

    def _redaction_reason(self, scope: str, report: RedactionReport) -> str:
        if not report.redacted:
            return f"{scope} accepted; no redaction rules fired"
        rules = ", ".join(report.rules_fired())
        return f"{scope} accepted; redaction rules fired: {rules}"

    # -- systems -----------------------------------------------------------------

    def ingest_system(self, session: Session, raw: Any, *, request_id: str) -> IngestResult:
        error = self._size_check(raw, request_id)
        if error is not None:
            return IngestResult("rejected", error=error)
        obj, error = self._decode(raw, request_id)
        if error is not None:
            return IngestResult("rejected", error=error)
        manifest, error = self._validate(SystemManifestInput, obj, request_id)
        if manifest is None:
            return IngestResult("rejected", error=error)
        redacted, report, error = self._redact(
            manifest.model_dump(mode="json", by_alias=True), request_id
        )
        if error is not None:
            return IngestResult("rejected", error=error)
        manifest, error = self._validate(SystemManifestInput, redacted, request_id)
        if manifest is None:
            return IngestResult("rejected", error=error)
        system = MonitoredSystem(
            id=manifest.id or new_system_id(),
            schema_version=manifest.schema_version,
            name=manifest.name,
            kind=manifest.kind,
            environment=manifest.environment,
            owner=manifest.owner,
            capabilities=list(manifest.capabilities),
            adapter=manifest.adapter,
            metadata=dict(manifest.metadata),
            created_at=manifest.created_at or utc_now(),
            status=manifest.status,
        )
        stored = _register_system(session, system, request_id=request_id)
        # ``register_system`` returns the existing row on a replay, so the caller cannot tell a
        # replay from a fresh insert by comparing ids alone -- a manifest whose id already matches
        # the stored row is exactly the replay case, and reporting it as "created" makes a second
        # seed run look like a write. The newest audit entry is the authority: this call appended
        # it, and a replay writes ``system.registration_replayed`` while an insert writes
        # ``system.registered``.
        newest = AuditRepository(session).last()
        replayed = newest is not None and newest.action == "system.registration_replayed"
        status: IngestStatus = "duplicate" if replayed else "created"
        return IngestResult(status=status, record_id=stored.id, record=stored)

    # -- events ------------------------------------------------------------------

    def ingest_event(
        self,
        session: Session,
        raw: Any,
        *,
        request_id: str,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        error = self._size_check(raw, request_id)
        if error is not None:
            return IngestResult("rejected", error=error)
        obj, error = self._decode(raw, request_id)
        if error is not None:
            return IngestResult("rejected", error=error)
        event_input, error = self._validate(EventInput, obj, request_id)
        if event_input is None:
            return IngestResult("rejected", error=error)
        if event_input.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            return IngestResult(
                "rejected",
                error=error_for(
                    ErrorCode.UNSUPPORTED_SCHEMA_VERSION,
                    request_id=request_id,
                    message=f"schema_version {event_input.schema_version!r} is not supported.",
                ),
            )
        redacted, report, error = self._redact(
            event_input.model_dump(mode="json", by_alias=True), request_id
        )
        if error is not None:
            return IngestResult("rejected", error=error)
        event_input, error = self._validate(EventInput, redacted, request_id)
        if event_input is None:
            return IngestResult(
                "rejected",
                error=error_for(ErrorCode.REDACTION_FAILED, request_id=request_id),
            )
        request_hash = content_hash(event_input.model_dump(mode="json", by_alias=True))
        keys = idempotency.candidate_keys(
            producer_event_id=event_input.producer_event_id, idempotency_key=idempotency_key
        )
        if keys:
            decision = idempotency.evaluate(
                session, scope="event", keys=keys, request_hash=request_hash
            )
            if decision.status == "duplicate":
                return IngestResult(
                    status="duplicate",
                    record_id=decision.record_id,
                    error=error_for(
                        ErrorCode.DUPLICATE_EVENT,
                        request_id=request_id,
                        message=(
                            "Event was already accepted as record "
                            f"{decision.record_id}; no new row was created."
                        ),
                    ),
                )
            if decision.status == "conflict":
                return rejected(
                    ErrorCode.IDEMPOTENCY_KEY_REUSED,
                    request_id=request_id,
                    message=(
                        "Idempotency key was reused with a different payload; existing record "
                        f"{decision.record_id}."
                    ),
                )

        event = Event(
            id=event_input.id or new_event_id(),
            schema_version=event_input.schema_version,
            source_system_id=event_input.source_system_id,
            event_type=event_input.event_type,
            occurred_at=event_input.occurred_at,
            received_at=utc_now(),
            correlation_id=event_input.correlation_id,
            parent_event_id=event_input.parent_event_id,
            actor=event_input.actor,
            subject=event_input.subject,
            payload=dict(event_input.payload),
            source_ref=event_input.source_ref,
            content_hash=_ZERO_DIGEST,
            trust_class=event_input.trust_class,
            labels=list(event_input.labels),
            retention_class=event_input.retention_class,
        )
        event = event.model_copy(update={"content_hash": event.calculated_content_hash()})

        EventRepository(session).add(event)
        if keys:
            idempotency.reserve(
                session,
                scope="event",
                keys=keys,
                record_id=event.id,
                request_hash=request_hash,
                rationale=self._redaction_reason("event", report),
            )
        self._audit(
            session,
            action="event.ingested",
            target_type="event",
            target_id=event.id,
            request_id=request_id,
            correlation_id=event.correlation_id,
            source_system_id=event.source_system_id,
            reason=self._redaction_reason("event", report),
        )
        return IngestResult(status="created", record_id=event.id, record=event)

    # -- claims ------------------------------------------------------------------

    def ingest_claim(
        self,
        session: Session,
        raw: Any,
        *,
        request_id: str,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        error = self._size_check(raw, request_id)
        if error is not None:
            return IngestResult("rejected", error=error)
        obj, error = self._decode(raw, request_id)
        if error is not None:
            return IngestResult("rejected", error=error)
        claim_input, error = self._validate(ClaimInput, obj, request_id)
        if claim_input is None:
            return IngestResult("rejected", error=error)
        redacted, report, error = self._redact(
            claim_input.model_dump(mode="json", by_alias=True), request_id
        )
        if error is not None:
            return IngestResult("rejected", error=error)
        claim_input, error = self._validate(ClaimInput, redacted, request_id)
        if claim_input is None:
            return IngestResult(
                "rejected",
                error=error_for(ErrorCode.REDACTION_FAILED, request_id=request_id),
            )
        request_hash = content_hash(claim_input.model_dump(mode="json", by_alias=True))
        keys = idempotency.candidate_keys(
            producer_event_id=claim_input.producer_event_id, idempotency_key=idempotency_key
        )
        if keys:
            decision = idempotency.evaluate(
                session, scope="claim", keys=keys, request_hash=request_hash
            )
            if decision.status == "duplicate":
                return IngestResult(
                    status="duplicate",
                    record_id=decision.record_id,
                    error=error_for(
                        ErrorCode.DUPLICATE_EVENT,
                        request_id=request_id,
                        message=(
                            "Claim was already accepted as record "
                            f"{decision.record_id}; no new row was created."
                        ),
                    ),
                )
            if decision.status == "conflict":
                return rejected(
                    ErrorCode.IDEMPOTENCY_KEY_REUSED,
                    request_id=request_id,
                    message=(
                        "Idempotency key was reused with a different payload; existing record "
                        f"{decision.record_id}."
                    ),
                )
        try:
            claim = Claim(
                id=claim_input.id or new_claim_id(),
                schema_version=claim_input.schema_version,
                statement=claim_input.statement,
                claimant_system_id=claim_input.claimant_system_id,
                claimant_principal_id=claim_input.claimant_principal_id,
                claim_type=claim_input.claim_type,
                scope=claim_input.scope,
                created_at=claim_input.created_at or utc_now(),
                source_event_ids=list(claim_input.source_event_ids),
                status=claim_input.status,
                retention_class=claim_input.retention_class,
            )
        except ValidationError as exc:
            details = _field_details(exc)
            if _has_unscoped_claim(exc):
                return IngestResult(
                    "rejected",
                    error=error_for(
                        ErrorCode.UNSCOPED_CLAIM, request_id=request_id, field_details=details
                    ),
                )
            return IngestResult(
                "rejected",
                error=error_for(
                    ErrorCode.SCHEMA_VALIDATION_FAILED, request_id=request_id, field_details=details
                ),
            )
        ClaimRepository(session).add(claim)
        if keys:
            idempotency.reserve(
                session,
                scope="claim",
                keys=keys,
                record_id=claim.id,
                request_hash=request_hash,
                rationale=self._redaction_reason("claim", report),
            )
        self._audit(
            session,
            action="claim.registered",
            target_type="claim",
            target_id=claim.id,
            request_id=request_id,
            correlation_id=None,
            source_system_id=claim.claimant_system_id,
            reason=self._redaction_reason("claim", report),
        )
        return IngestResult(status="created", record_id=claim.id, record=claim)

    # -- evidence ----------------------------------------------------------------

    def _validate_evidence_targets(
        self, evidence_input: EvidenceInput, request_id: str
    ) -> StructuredError | None:
        """Enforce the SSRF and path-traversal guards on an evidence target (SPEC §12).

        A producer-supplied ``source_ref`` URL and ``locator`` are untrusted data. The URL guard
        refuses a private or non-routable target unless the deployment has explicitly opted in;
        the locator guard refuses an absolute path or a ``..``/symlink escape out of the data
        directory unless absolute locators are explicitly allowed. Both refuse with the existing
        structured envelope, never by silently accepting the target.
        """

        if is_http_url(evidence_input.source_ref):
            # A host that does not resolve is not a fetch target: NYX records the reference at
            # registration and does not fetch it, so it is not an SSRF risk. The fetch guard
            # (validate_evidence_url) refuses it at any future fetch boundary, where the
            # fail-closed rule applies. A host that *does* resolve to a private target is
            # refused here, at the ingestion edge.
            host = urlsplit(evidence_input.source_ref).hostname or ""
            if host and host_resolves(host):
                try:
                    validate_evidence_url(
                        evidence_input.source_ref,
                        allow_private=self._settings.evidence_allow_private_urls,
                    )
                except UrlFetchDenied as exc:
                    return error_for(
                        ErrorCode.SCHEMA_VALIDATION_FAILED,
                        request_id=request_id,
                        message=str(exc),
                        field_details=[
                            FieldDetail(field="/source_ref", issue=str(exc), code="invalid_target")
                        ],
                    )

        locator = evidence_input.locator
        if (
            locator
            and is_path_like(locator)
            and not self._settings.evidence_allow_absolute_locators
        ):
            try:
                resolve_within(self._settings.data_dir, locator)
            except PathTraversalError as exc:
                return error_for(
                    ErrorCode.SCHEMA_VALIDATION_FAILED,
                    request_id=request_id,
                    message=str(exc),
                    field_details=[
                        FieldDetail(field="/locator", issue=str(exc), code="invalid_target")
                    ],
                )
        return None

    def ingest_evidence(
        self,
        session: Session,
        raw: Any,
        *,
        request_id: str,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        error = self._size_check(raw, request_id)
        if error is not None:
            return IngestResult("rejected", error=error)
        obj, error = self._decode(raw, request_id)
        if error is not None:
            return IngestResult("rejected", error=error)
        evidence_input, error = self._validate(EvidenceInput, obj, request_id)
        if evidence_input is None:
            return IngestResult("rejected", error=error)
        redacted, report, error = self._redact(
            evidence_input.model_dump(mode="json", by_alias=True), request_id
        )
        if error is not None:
            return IngestResult("rejected", error=error)
        evidence_input, error = self._validate(EvidenceInput, redacted, request_id)
        if evidence_input is None:
            return IngestResult(
                "rejected",
                error=error_for(ErrorCode.REDACTION_FAILED, request_id=request_id),
            )
        error = self._validate_evidence_targets(evidence_input, request_id)
        if error is not None:
            return IngestResult("rejected", error=error)
        request_hash = content_hash(evidence_input.model_dump(mode="json", by_alias=True))
        keys = idempotency.candidate_keys(
            producer_event_id=evidence_input.producer_event_id, idempotency_key=idempotency_key
        )
        if keys:
            decision = idempotency.evaluate(
                session, scope="evidence", keys=keys, request_hash=request_hash
            )
            if decision.status == "duplicate":
                return IngestResult(
                    status="duplicate",
                    record_id=decision.record_id,
                    error=error_for(
                        ErrorCode.DUPLICATE_EVENT,
                        request_id=request_id,
                        message=(
                            "Evidence was already accepted as record "
                            f"{decision.record_id}; no new row was created."
                        ),
                    ),
                )
            if decision.status == "conflict":
                return rejected(
                    ErrorCode.IDEMPOTENCY_KEY_REUSED,
                    request_id=request_id,
                    message=(
                        "Idempotency key was reused with a different payload; existing record "
                        f"{decision.record_id}."
                    ),
                )
        try:
            evidence = EvidenceItem(
                id=evidence_input.id or new_evidence_id(),
                schema_version=evidence_input.schema_version,
                kind=evidence_input.kind,
                title=evidence_input.title,
                description=evidence_input.description,
                source_system_id=evidence_input.source_system_id,
                source_name=evidence_input.source_name,
                source_ref=evidence_input.source_ref,
                locator=evidence_input.locator,
                scope=evidence_input.scope,
                captured_at=evidence_input.captured_at,
                valid_from=evidence_input.valid_from,
                valid_until=evidence_input.valid_until,
                content_hash=evidence_input.content_hash,
                integrity_method=evidence_input.integrity_method,
                producer_identity=evidence_input.producer_identity,
                verification=evidence_input.verification,
                relevance_to_claim=evidence_input.relevance_to_claim,
                relevance_to_finding=evidence_input.relevance_to_finding,
                relevance_note=evidence_input.relevance_note,
                limitations=list(evidence_input.limitations),
                sensitivity=evidence_input.sensitivity,
                access_policy_ref=evidence_input.access_policy_ref,
                retention_class=evidence_input.retention_class,
            )
        except ValidationError as exc:
            return IngestResult(
                "rejected",
                error=error_for(
                    ErrorCode.SCHEMA_VALIDATION_FAILED,
                    request_id=request_id,
                    field_details=_field_details(exc),
                ),
            )
        EvidenceRepository(session).add(evidence)
        if keys:
            idempotency.reserve(
                session,
                scope="evidence",
                keys=keys,
                record_id=evidence.id,
                request_hash=request_hash,
                rationale=self._redaction_reason("evidence", report),
            )
        self._audit(
            session,
            action="evidence.registered",
            target_type="evidence",
            target_id=evidence.id,
            request_id=request_id,
            correlation_id=None,
            source_system_id=evidence.source_system_id,
            reason=self._redaction_reason("evidence", report),
        )
        return IngestResult(status="created", record_id=evidence.id, record=evidence)

    # -- audit -------------------------------------------------------------------

    def _audit(
        self,
        session: Session,
        *,
        action: str,
        target_type: str,
        target_id: str,
        request_id: str,
        correlation_id: str | None,
        source_system_id: str | None,
        reason: str,
    ) -> None:
        actor = (
            PrincipalRef(id=source_system_id, kind=PrincipalKind.SYSTEM)
            if source_system_id is not None
            else _ACTOR
        )
        AuditWriter(session).append(
            AuditEntryInput(
                actor=actor,
                action=action,
                target_type=target_type,
                target_id=target_id,
                request_id=request_id,
                correlation_id=correlation_id,
                outcome=AuditOutcome.SUCCESS,
                reason=reason,
            )
        )
