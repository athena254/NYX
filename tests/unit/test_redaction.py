"""Redaction rules, report shape, and the no-value-leak guarantee (SPEC §12, scenario 14)."""

from __future__ import annotations

import json
from typing import Any

from nyx.ingestion.redaction import PLACEHOLDER_PREFIX, redact

_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
_BEARER = "Bearer abcdefghijklmnop123456"
_URL_CREDENTIALS = "https://user:supersecret@example.invalid/path"
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_API_KEY = "sk_live_0123456789abcdef"
_HIGH_ENTROPY = "5f4dcc3b5aa765d61d8327deb882cf99a1b2c3d4e5f60718aabbccddeeff0011"

_POSITIVE_SAMPLES: list[tuple[str, str]] = [
    ("private_key_block", _PRIVATE_KEY),
    ("jwt", f"token {_JWT}"),
    ("bearer_token", _BEARER),
    ("url_credentials", _URL_CREDENTIALS),
    ("aws_access_key_id", f"key {_AWS_KEY}"),
    ("api_key_like", _API_KEY),
    ("secret_assignment", "password=hunter2secret"),
]


def test_each_pattern_rule_fires_on_a_positive_sample() -> None:
    for rule, sample in _POSITIVE_SAMPLES:
        redacted, report = redact({"note": sample})

        assert rule in report.rules_fired(), f"{rule} did not fire on {sample!r}"
        assert PLACEHOLDER_PREFIX in json.dumps(redacted)
        assert sample not in json.dumps(redacted)


def test_secret_key_value_rule_fires_for_json_key_value() -> None:
    redacted, report = redact({"credentials": {"password": "hunter2"}})

    assert "secret_key_value" in report.rules_fired()
    assert redacted["credentials"].startswith(PLACEHOLDER_PREFIX)
    assert "hunter2" not in json.dumps(redacted)


def test_high_entropy_value_rule_fires_in_a_value_position() -> None:
    redacted, report = redact({"blob": _HIGH_ENTROPY})

    assert "high_entropy_value" in report.rules_fired()
    assert redacted["blob"].startswith(PLACEHOLDER_PREFIX)


def test_high_entropy_rule_does_not_destroy_identifier_fields() -> None:
    commit = "a" * 40
    digest = "sha256:" + "b" * 64

    redacted, report = redact({"commit": commit, "content_hash": digest})

    assert redacted == {"commit": commit, "content_hash": digest}
    assert not report.redacted


def test_clean_payload_is_untouched() -> None:
    payload: dict[str, Any] = {
        "message": "hello world",
        "count": 3,
        "nested": {"name": "alice", "tags": ["a", "b"]},
        "enabled": True,
    }

    redacted, report = redact(payload)

    assert redacted == payload
    assert report.redacted is False
    assert report.rules_fired() == []


def test_report_names_rules_and_paths_but_never_values() -> None:
    payload = {"headers": {"authorization": _BEARER}, "body": {"data": _HIGH_ENTROPY}}

    _, report = redact(payload)
    rendered = json.dumps(report.to_dict())

    assert report.paths()
    assert "secret_key_value" in report.rules_fired()
    assert _BEARER not in rendered
    assert _HIGH_ENTROPY not in rendered
    assert "hunter" not in rendered


def test_redaction_does_not_mutate_the_input() -> None:
    payload = {"token": "super-secret-value"}

    redacted, _ = redact(payload)

    assert payload == {"token": "super-secret-value"}
    assert redacted["token"].startswith(PLACEHOLDER_PREFIX)
