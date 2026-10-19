"""Tests for log redaction: extra attributes must be redacted before JSON output.

Covers the fix for audit finding A-2 (V-1): ``JsonFormatter.format`` must pass extra
attribute values through the ingestion redaction rules before writing them to the
JSON log line.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nyx.observability.logging import JsonFormatter

_SECRET = "sk-live-abcdef1234567890abcdef1234567890"  # noqa: S105


def _format(record: logging.LogRecord) -> dict[str, Any]:
    """Format a record with JsonFormatter and return the parsed JSON payload."""
    return json.loads(JsonFormatter().format(record))


def _make_record(msg: str = "test message", **extra: Any) -> logging.LogRecord:
    """Build a LogRecord with extra attributes set directly on it."""
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_api_key_in_extra_is_redacted() -> None:
    """A secret passed as an extra attribute must not appear in the JSON output."""
    record = _make_record("ingesting event", api_key=_SECRET)
    payload = _format(record)
    rendered = json.dumps(payload)

    assert _SECRET not in rendered
    assert payload["api_key"].startswith("[REDACTED:")


def test_secret_nested_in_dict_extra_is_redacted() -> None:
    """A secret nested inside a dict extra value must also be redacted."""
    record = _make_record("event", metadata={"token": _SECRET, "note": "hello"})
    payload = _format(record)
    rendered = json.dumps(payload)

    assert _SECRET not in rendered
    assert payload["metadata"]["token"].startswith("[REDACTED:")
    assert payload["metadata"]["note"] == "hello"


def test_secret_nested_in_list_extra_is_redacted() -> None:
    """A secret inside a list extra value must be redacted."""
    record = _make_record("event", items=[_SECRET, "benign"])
    payload = _format(record)
    rendered = json.dumps(payload)

    assert _SECRET not in rendered
    assert payload["items"][0].startswith("[REDACTED:")
    assert payload["items"][1] == "benign"


def test_benign_extras_are_unchanged() -> None:
    """Non-secret extras like request_id and status_code must pass through intact."""
    record = _make_record(
        "request handled",
        request_id="req_123",
        status_code=200,
        method="GET",
        path="/api/v1/ingest",
        duration_ms=42.5,
    )
    payload = _format(record)

    assert payload["request_id"] == "req_123"
    assert payload["status_code"] == 200
    assert payload["method"] == "GET"
    assert payload["path"] == "/api/v1/ingest"
    assert payload["duration_ms"] == 42.5


def test_message_field_is_present_and_correct() -> None:
    """The normal message field must still be present and unmodified."""
    record = _make_record("hello world", request_id="req_123")
    payload = _format(record)

    assert payload["message"] == "hello world"
