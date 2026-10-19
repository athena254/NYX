"""URL paths must stay traceable, and real secrets in URLs must still be caught.

The high-entropy heuristic's character class deliberately includes ``/`` so that base64
values match -- which also made a plain URL path match, because a path such as
``/api/v1/events/evt_01M5B1...`` is long, alphanumeric and slash-separated.

Redacting the path in the access log destroys request tracing: the operator can see that a
request happened but not which resource it touched. For an audit service that is a serious
loss, so paths are exempted from the *entropy heuristic* while the named secret rules still
apply to them.
"""

from __future__ import annotations

from nyx.ingestion.redaction import PLACEHOLDER_PREFIX, redact

PATH = "/api/v1/events/evt_01M5B1PBSQSVMTFP9KC237XH5R"
URL = "https://ci.example.invalid/run/01M5B1PBSQSVMTFP9KC237XH5R"
BASE64_BLOB = "QWxsIHlvdXIgYmFzZTY0IGFyZSBiZWxvbmcgdG8gdXMgbm93IG9rYXk="


def test_url_path_is_not_redacted() -> None:
    """A request path must survive redaction so the access log stays traceable."""

    redacted, _ = redact({"path": PATH})

    assert redacted["path"] == PATH, "the request path was redacted; tracing is broken"


def test_full_url_is_not_redacted() -> None:
    """A URL is likewise not an opaque blob."""

    redacted, _ = redact({"source_ref": URL})

    assert redacted["source_ref"] == URL


def test_high_entropy_blob_is_still_redacted() -> None:
    """The exemption must not weaken the actual heuristic."""

    redacted, _ = redact({"value": BASE64_BLOB})

    assert redacted["value"].startswith(PLACEHOLDER_PREFIX), "a real blob escaped redaction"


def test_secret_embedded_in_url_is_still_redacted() -> None:
    """The named rules still run against URL-shaped values, so real secrets are caught."""

    # ruff S105 fires on the *name* `secret`; this is a synthetic value used to prove
    # redaction works, so name it for what it is rather than suppressing the rule.
    leaky = "sk-live-abcdef1234567890abcdef1234567890"
    url = f"https://example.invalid/callback?api_key={leaky}"

    redacted, _ = redact({"source_ref": url})

    assert leaky not in str(redacted), "a secret inside a URL escaped redaction"
