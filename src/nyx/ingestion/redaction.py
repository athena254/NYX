"""Secret redaction that runs before persistence and before any model call (SPEC §12).

The module exposes a fixed, named rule set and a single entry point:

``redact(payload) -> (redacted_payload, RedactionReport)``

The returned report lists **which rule names fired and at which JSON paths**. It never
contains the secret values themselves, so it is safe to log, store, and return to a caller.

Design notes
------------
- Rules are conservative but default to redacting on doubt for the recognised secret classes
  (bearer tokens, API-key-shaped strings, private-key blocks, JWTs, URL credentials,
  key/secret assignments, AWS-style access key ids, and long high-entropy blobs in a value
  position).
- A value whose *key name* looks like a secret is replaced wholesale even when it does not
  match a pattern. This closes the common ``{"password": "hunter2"}`` case.
- High-entropy detection is key-aware: recognised non-secret identifier keys such as
  ``commit``, ``sha``, and ``digest`` are exempt so that content hashes and commit ids are not
  destroyed. This is a deliberate trade-off, documented in ``docs/privacy-and-retention.md``.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger("nyx.ingestion.redaction")

PLACEHOLDER_PREFIX = "[REDACTED:"

_KEY_MARKERS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "client_secret",
    "credential",
    "private_key",
    "auth",
)

_IDENTIFIER_KEY_MARKERS = (
    "commit",
    "sha",
    "digest",
    "hash",
    "revision",
    "checksum",
    "artifact",
    "version",
    "content_hash",
    "id",
    "uuid",
    "trace",
    "span",
    # Display and identity fields. ``name`` is here because a system manifest's ``name`` is a
    # schema-constrained identifier (``^[a-z0-9][a-z0-9._-]*$``), and the high-entropy rule was
    # firing on a hyphenated slug long enough to look opaque -- turning
    # ``eval-acceptance-...-0572c956`` into ``[REDACTED:high_entropy_value]``, which then failed
    # the very schema the payload had already passed. A field that a schema constrains to a slug
    # cannot hold a secret, so the heuristic has no business touching it.
    "name",
    "title",
    "slug",
    "label",
)


@dataclass(frozen=True)
class RedactionFinding:
    """One redaction event: which rule fired at which path (never the value)."""

    rule: str
    path: str
    count: int = 1


@dataclass
class RedactionReport:
    """A value-free record of what redaction changed."""

    enabled: bool = True
    findings: list[RedactionFinding] = field(default_factory=list)

    def add(self, rule: str, path: str, count: int = 1) -> None:
        self.findings.append(RedactionFinding(rule=rule, path=path, count=count))

    @property
    def redacted(self) -> bool:
        """Return whether any rule fired."""

        return bool(self.findings)

    def rules_fired(self) -> list[str]:
        """Return the sorted, de-duplicated names of the rules that fired."""

        return sorted({finding.rule for finding in self.findings})

    def paths(self) -> list[str]:
        """Return the sorted, de-duplicated JSON paths that were redacted."""

        return sorted({finding.path for finding in self.findings})

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, value-free representation of the report."""

        return {
            "enabled": self.enabled,
            "redacted": self.redacted,
            "rules": self.rules_fired(),
            "findings": [{"rule": f.rule, "path": f.path, "count": f.count} for f in self.findings],
        }


_PATTERN_RULES: tuple[tuple[str, re.Pattern[str], str | None], ...] = (
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        None,
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
        None,
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        None,
    ),
    (
        "url_credentials",
        re.compile(
            r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^/\s:@]+):(?P<password>[^/\s:@]+)@"
        ),
        "credentials",
    ),
    (
        "aws_access_key_id",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        None,
    ),
    (
        "api_key_like",
        re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{12,}\b|\bsk-[A-Za-z0-9]{20,}\b"),
        None,
    ),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key"
            r"|client[_-]?secret|auth[_-]?token)(\s*[:=]\s*)([\"']?)([^\s\"',;]{4,})"
        ),
        "assignment",
    ),
)


def _placeholder(rule: str) -> str:
    return f"{PLACEHOLDER_PREFIX}{rule}]"


def _normalise_key(key: str) -> str:
    return key.lower().replace("-", "_").replace(" ", "_")


def _is_secret_key(key: str) -> bool:
    normalised = _normalise_key(key)
    collapsed = normalised.replace("_", "")
    return any(marker in normalised for marker in _KEY_MARKERS) or any(
        marker.replace("_", "") in collapsed for marker in _KEY_MARKERS
    )


def _is_identifier_key(key: str) -> bool:
    normalised = _normalise_key(key)
    return any(marker in normalised for marker in _IDENTIFIER_KEY_MARKERS)


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_like_url_or_path(value: str) -> bool:
    """Return whether ``value`` is a URL or URL path rather than an opaque blob.

    The high-entropy heuristic exists to catch *unlabelled opaque* values: base64 keys, hex
    tokens, and the like. Its character class deliberately includes ``/`` so base64 is
    matched -- which also makes a plain URL path match, because a path such as
    ``/api/v1/events/evt_01M5B1...`` is long, alphanumeric and slash-separated.

    Flagging that is a net loss: it destroys request tracing in the access log, and an audit
    service that cannot say which resource was requested has lost more than it gained. A path
    is neither opaque nor secret by nature. Secrets that genuinely appear in a URL are still
    caught by the *named* rules -- bearer token, API key, JWT, URL credentials -- which run
    before this heuristic.
    """

    return value.startswith("/") or "://" in value


def _looks_high_entropy(value: str, *, key_hint: str | None) -> bool:
    if len(value) < 32:
        return False
    if value.startswith("sha256:") or value.startswith(PLACEHOLDER_PREFIX):
        return False
    if _looks_like_url_or_path(value):
        return False
    if key_hint is not None and _is_identifier_key(key_hint):
        return False
    if re.fullmatch(r"[0-9a-fA-F]+", value):
        return len(value) >= 40 and _shannon_entropy(value) >= 3.0
    if re.fullmatch(r"[A-Za-z0-9+/=_-]+", value):
        return len(value) >= 40 and _shannon_entropy(value) >= 3.5
    return False


def _pointer_escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _redact_string(value: str, path: str, report: RedactionReport) -> str:
    for rule, pattern, _ in _PATTERN_RULES:
        if rule == "url_credentials":
            if pattern.search(value):
                value = pattern.sub(_credentials_replacement, value)
                report.add(rule, path)
            continue
        if rule == "secret_assignment":
            if pattern.search(value):
                value = pattern.sub(_assignment_replacement, value)
                report.add(rule, path)
            continue
        if pattern.search(value):
            value = pattern.sub(_placeholder(rule), value)
            report.add(rule, path)
    return value


def _credentials_replacement(match: re.Match[str]) -> str:
    scheme = match.group("scheme")
    user = match.group("user")
    return f"{scheme}{user}:{_placeholder('url_credentials')}@"


def _assignment_replacement(match: re.Match[str]) -> str:
    key = match.group(1)
    separator = match.group(2)
    quote = match.group(3)
    return f"{key}{separator}{quote}{_placeholder('secret_assignment')}"


def _redact(value: Any, path: str, report: RedactionReport, key_hint: str | None) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}/{_pointer_escape(key)}"
            if _is_secret_key(key):
                result[raw_key] = _placeholder("secret_key_value")
                report.add("secret_key_value", child_path)
            else:
                result[raw_key] = _redact(child, child_path, report, key)
        return result
    if isinstance(value, list):
        return [
            _redact(child, f"{path}/{index}", report, key_hint) for index, child in enumerate(value)
        ]
    if isinstance(value, str):
        redacted = _redact_string(value, path, report)
        if _looks_high_entropy(redacted, key_hint=key_hint):
            report.add("high_entropy_value", path)
            return _placeholder("high_entropy_value")
        return redacted
    return value


def redact(payload: Any) -> tuple[Any, RedactionReport]:
    """Return a redacted copy of ``payload`` and a value-free report.

    ``payload`` may be any JSON-compatible value. The input is not mutated: containers are
    rebuilt. Rule names and JSON paths are recorded; secret values never are.
    """

    report = RedactionReport(enabled=True)
    try:
        redacted = _redact(payload, "", report, None)
    except Exception:  # noqa: BLE001 - fail closed, never propagate a value
        _logger.warning("redaction failed; refusing to process the payload")
        raise RedactionError("redaction failed") from None
    return redacted, report


class RedactionError(RuntimeError):
    """Raised when redaction cannot be completed safely.

    The ingestion gateway translates this into ``REDACTION_FAILED`` and refuses the write.
    """
