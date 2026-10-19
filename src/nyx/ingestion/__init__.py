"""Ingestion gateway: validate, limit, redact, deduplicate, persist, audit."""

from nyx.ingestion.errors import FieldDetail, IngestResult, StructuredError
from nyx.ingestion.gateway import IngestionGateway
from nyx.ingestion.limits import LimitExceededError, enforce_batch_size, enforce_payload_size
from nyx.ingestion.redaction import RedactionReport, redact
from nyx.ingestion.service import IngestionService

__all__ = [
    "FieldDetail",
    "IngestResult",
    "IngestionGateway",
    "IngestionService",
    "LimitExceededError",
    "RedactionReport",
    "StructuredError",
    "enforce_batch_size",
    "enforce_payload_size",
    "redact",
]
