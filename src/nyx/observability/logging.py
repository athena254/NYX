"""JSON logging with a request-id context variable (SPEC §16)."""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

from nyx.ingestion.redaction import RedactionError, redact

request_id_var: ContextVar[str] = ContextVar("nyx_request_id", default="-")


def set_request_id(request_id: str) -> Token[str]:
    """Bind a request id to the current context; returns the reset token."""

    return request_id_var.set(request_id)


_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
}


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON line including the request id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        if extras:
            try:
                redacted_extras, _ = redact(extras)
            except RedactionError:
                redacted_extras = {}
            for key, value in redacted_extras.items():
                payload.setdefault(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Add the JSON formatter to the root logger, once.

    The handler is **added**, not substituted. ``root.handlers = [handler]`` discards every handler
    another component installed -- a test runner's capture handler, uvicorn's, a library's -- and a
    handler that captured a stream which has since been closed raises on emit, which surfaces as a
    ``--- Logging error ---`` block on stderr. Adding ours and leaving the rest alone is both safer
    and idempotent: a second call is a no-op rather than a second handler.
    """

    root = logging.getLogger()
    root.setLevel(level.upper())
    already_configured = any(
        isinstance(handler, logging.StreamHandler) and isinstance(handler.formatter, JsonFormatter)
        for handler in root.handlers
    )
    if already_configured:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
