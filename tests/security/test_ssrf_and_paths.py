"""SSRF and path-traversal guards for evidence targets (SPEC §12, ADR 0006).

Scenario 15/16's structural controls, asserted at the boundary where they are enforced:

* ``is_private_target`` / ``validate_evidence_url`` refuse loopback, RFC1918, link-local, CGNAT,
  IPv6 ULA/``::1``, multicast/reserved, a name that resolves to a private address (the
  DNS-rebinding shape), and a name that does not resolve at all (fail closed);
* ``resolve_within`` refuses ``..`` escapes, absolute paths, and symlink escapes;
* the ingestion gateway refuses a private ``source_ref`` and an escaping ``locator`` with the
  existing structured envelope, and honours the two explicit opt-ins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nyx.config import Settings
from nyx.ingestion.paths import PathTraversalError, is_path_like, resolve_within
from nyx.ingestion.service import IngestionService
from nyx.ingestion.url_fetch import UrlFetchDenied, is_private_target, validate_evidence_url
from nyx.storage.db import SessionFactory

_API = "/api/v1"
_NOW = "2026-10-20T09:00:00Z"

#: A literal public address: the "allowed" case must not depend on the test host's DNS.
_PUBLIC_IP = "93.184.216.34"

_PRIVATE_TARGETS: tuple[tuple[str, str], ...] = (
    ("http://127.0.0.1/admin", "127.0.0.1"),
    ("https://10.0.0.5/latest/meta-data/", "10.0.0.5"),
    ("https://172.16.4.4/", "172.16.4.4"),
    ("https://192.168.1.1/", "192.168.1.1"),
    ("https://169.254.169.254/latest/meta-data/", "169.254.169.254"),
    ("https://100.64.0.1/", "100.64.0.1"),
    ("https://[::1]/", "::1"),
    ("https://[fe80::1]/", "fe80::1"),
    ("https://[fc00::1]/", "fc00::1"),
    ("https://[ff02::1]/", "ff02::1"),
    ("https://224.0.0.1/", "224.0.0.1"),
    ("https://0.0.0.0/", "0.0.0.0"),  # noqa: S104 - a reserved target, not a bind address
)


# --------------------------------------------------------------------------- URL guard


@pytest.mark.parametrize(("url", "host"), _PRIVATE_TARGETS)
def test_private_targets_are_refused(url: str, host: str) -> None:
    assert is_private_target(host) is True
    with pytest.raises(UrlFetchDenied):
        validate_evidence_url(url, allow_private=False)


@pytest.mark.parametrize(("url", "host"), _PRIVATE_TARGETS)
def test_private_targets_are_allowed_only_when_explicitly_configured(url: str, host: str) -> None:
    # The opt-in is a deployment-wide decision, never inferred from the URL.
    validate_evidence_url(url, allow_private=True)


def test_a_public_target_is_allowed() -> None:
    assert is_private_target(_PUBLIC_IP) is False
    validate_evidence_url(f"http://{_PUBLIC_IP}/evidence/1", allow_private=False)


def test_a_name_resolving_to_a_private_address_is_refused() -> None:
    """The DNS-rebinding shape: a public-looking name that resolves to a loopback address."""

    assert is_private_target("localhost") is True
    with pytest.raises(UrlFetchDenied):
        validate_evidence_url("http://localhost/admin", allow_private=False)


def test_resolution_failure_is_refused_fail_closed() -> None:
    assert is_private_target("no-such-host.nyx.invalid") is True
    with pytest.raises(UrlFetchDenied):
        validate_evidence_url("https://no-such-host.nyx.invalid/x", allow_private=False)


@pytest.mark.parametrize(
    "url",
    (
        "file:///etc/passwd",
        "ftp://example.com/evidence",
        "gopher://127.0.0.1/",
        "://missing-scheme",
    ),
)
def test_non_http_schemes_are_refused(url: str) -> None:
    with pytest.raises(UrlFetchDenied):
        validate_evidence_url(url, allow_private=False)


def test_a_url_without_a_host_is_refused() -> None:
    with pytest.raises(UrlFetchDenied):
        validate_evidence_url("http:///path-only", allow_private=False)


# --------------------------------------------------------------------------- path guard


def test_parent_directory_escape_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "../secret.txt")
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "nested/../../secret.txt")
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "..")


def test_absolute_paths_are_refused(tmp_path: Path) -> None:
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "/etc/passwd")
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "C:/Windows/system32/config/SAM")
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "\\\\server\\share\\file")


def test_symlink_escape_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("not yours", encoding="utf-8")
    try:
        (tmp_path / "link").symlink_to(outside, target_is_directory=False)
    except OSError:
        pytest.skip("symlink creation is not permitted on this platform")

    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "link")


def test_a_path_inside_the_base_is_allowed(tmp_path: Path) -> None:
    resolved = resolve_within(tmp_path, "logs/app.log")

    assert resolved == (tmp_path / "logs" / "app.log").resolve()


def test_opaque_references_are_not_paths() -> None:
    assert is_path_like("https://ci.example.invalid/run/1") is False
    assert is_path_like("s3://bucket/key") is False
    assert is_path_like("job:required-integration#L1-L200") is False
    assert is_path_like("git:abc123def456") is False
    assert is_path_like("logs/app.log") is True
    assert is_path_like("/etc/passwd") is True
    assert is_path_like("../secret") is True


# --------------------------------------------------------------------------- wiring


def _evidence_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "ci_test_result",
        "title": "CI run",
        "description": "A CI test result.",
        "source_system_id": "sys_ssrf_paths",
        "source_ref": "https://ci.example.invalid/run/1",
        "captured_at": _NOW,
        "valid_from": _NOW,
        "integrity_method": "none",
        "relevance_to_claim": "clm_ssrf_paths",
        "limitations": ["Synthetic test evidence."],
        "sensitivity": "internal",
        "access_policy_ref": "test",
        "retention_class": "standard",
    }
    payload.update(overrides)
    return payload


def test_a_private_url_is_refused_at_registration(service: IngestionService) -> None:
    result = service.register_evidence(
        _evidence_payload(
            source_ref="http://169.254.169.254/latest/meta-data/", producer_event_id="ssrf-1"
        ),
        request_id="req-ssrf-1",
    )

    assert result.status == "rejected"
    assert result.record_id is None
    assert result.error is not None
    assert result.error.code == "SCHEMA_VALIDATION_FAILED"
    assert result.error.field_details[0].field == "/source_ref"
    assert "private" in result.error.message


def test_a_private_url_is_allowed_when_configured(
    session_factory: SessionFactory, settings: Settings
) -> None:
    relaxed = settings.model_copy(update={"evidence_allow_private_urls": True})
    service = IngestionService(session_factory, relaxed)

    result = service.register_evidence(
        _evidence_payload(
            source_ref="http://169.254.169.254/latest/meta-data/", producer_event_id="ssrf-2"
        ),
        request_id="req-ssrf-2",
    )

    assert result.status == "created", result.error


def test_a_traversal_locator_is_refused_at_registration(service: IngestionService) -> None:
    result = service.register_evidence(
        _evidence_payload(locator="../secret.txt", producer_event_id="path-1"),
        request_id="req-path-1",
    )

    assert result.status == "rejected"
    assert result.error is not None
    assert result.error.field_details[0].field == "/locator"
    assert "traversal" in result.error.message


def test_an_absolute_locator_is_refused_at_registration(service: IngestionService) -> None:
    result = service.register_evidence(
        _evidence_payload(locator="/etc/passwd", producer_event_id="path-2"),
        request_id="req-path-2",
    )

    assert result.status == "rejected"
    assert result.error is not None
    assert result.error.field_details[0].field == "/locator"


def test_a_relative_locator_inside_the_data_dir_is_accepted(
    service: IngestionService,
) -> None:
    result = service.register_evidence(
        _evidence_payload(locator="logs/app.log", producer_event_id="path-3"),
        request_id="req-path-3",
    )

    assert result.status == "created", result.error


def test_an_absolute_locator_is_allowed_when_configured(
    session_factory: SessionFactory, settings: Settings
) -> None:
    relaxed = settings.model_copy(update={"evidence_allow_absolute_locators": True})
    service = IngestionService(session_factory, relaxed)

    result = service.register_evidence(
        _evidence_payload(locator="/etc/passwd", producer_event_id="path-4"),
        request_id="req-path-4",
    )

    assert result.status == "created", result.error


def test_a_private_url_is_refused_over_http(client: TestClient) -> None:
    response = client.post(
        f"{_API}/evidence",
        json=_evidence_payload(source_ref="http://169.254.169.254/latest/meta-data/"),
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "SCHEMA_VALIDATION_FAILED"
    assert body["request_id"]
    # The refusal carries no evidence content.
    assert "latest/meta-data" not in response.text
