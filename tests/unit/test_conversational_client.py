"""The NYX HTTP client: the conversational shell's only path to NYX core.

The property under test is that this client is a *faithful, bounded* proxy: it forwards what a
user could do by hand, and NYX's own refusals arrive intact as typed errors rather than being
swallowed or paraphrased. That is what makes the MCP tool surface a real safety boundary rather
than a formality.
"""

from __future__ import annotations

import json

import httpx
import pytest

from nyx.conversational.client import NyxClient, NyxClientError, NyxUnavailableError


def _client(handler, base_url: str = "http://nyx.test") -> NyxClient:
    """Build a client whose HTTP layer is a mock transport."""

    return NyxClient(base_url, transport=httpx.MockTransport(handler))


def _json_response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_list_findings_unwraps_the_page() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _json_response({"items": [{"id": "fnd_1"}, {"id": "fnd_2"}], "next_cursor": None})

    findings = _client(handler).list_findings(status="detected", limit=25)

    assert [item["id"] for item in findings] == ["fnd_1", "fnd_2"]
    assert "status=detected" in str(seen["url"])
    assert "limit=25" in str(seen["url"])


def test_list_findings_tolerates_a_bare_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response([{"id": "fnd_1"}])

    assert [item["id"] for item in _client(handler).list_findings()] == ["fnd_1"]


def test_get_finding_returns_none_when_nyx_says_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"code": "NOT_FOUND", "message": "Finding not found.", "request_id": "r1"}, status=404
        )

    assert _client(handler).get_finding("fnd_missing") is None


def test_get_finding_returns_the_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"id": "fnd_1", "status": "detected"})

    assert _client(handler).get_finding("fnd_1") == {"id": "fnd_1", "status": "detected"}


def test_nyx_refusal_arrives_as_a_typed_error_with_its_own_code() -> None:
    """A capability refusal must survive as CAPABILITY_REQUIRED, not become prose."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "code": "CAPABILITY_REQUIRED",
                "message": "principal lacks capability 'findings.disposition'",
                "request_id": "r1",
            },
            status=403,
        )

    with pytest.raises(NyxClientError) as excinfo:
        _client(handler).get_history("fnd_1")

    assert excinfo.value.code == "CAPABILITY_REQUIRED"
    assert excinfo.value.status == 403


def test_unreachable_nyx_is_distinguished_from_a_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(NyxUnavailableError) as excinfo:
        _client(handler).list_findings()

    assert excinfo.value.code == "UNAVAILABLE"


def test_timeout_is_reported_as_such() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("deadline", request=request)

    with pytest.raises(NyxUnavailableError) as excinfo:
        _client(handler).list_findings()

    assert excinfo.value.code == "TIMEOUT"


def test_submit_rebuttal_sends_the_expected_body() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        seen["url"] = str(request.url)
        return _json_response({"id": "rbt_1", "requested_outcome": "withdrawn"})

    result = _client(handler).submit_rebuttal(
        finding_id="fnd_1",
        submitted_by="sys_ghost",
        argument="The evidence covers the claimed commit.",
        evidence_ids=["ev_1"],
        requested_outcome="withdrawn",
    )

    assert str(seen["url"]).endswith("/api/v1/findings/fnd_1/rebuttals")
    assert seen["body"] == {
        "submitted_by": "sys_ghost",
        "argument": "The evidence covers the claimed commit.",
        "evidence_ids": ["ev_1"],
        "requested_outcome": "withdrawn",
    }
    assert result["id"] == "rbt_1"


def test_request_verification_sends_the_expected_body() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return _json_response({"id": "vbj_1", "status": "queued"})

    _client(handler).request_verification(
        finding_id="fnd_1", check_id="critics.rerun-linked", requested_by="principal_operator"
    )

    assert seen["body"] == {
        "check_id": "critics.rerun-linked",
        "requested_by": "principal_operator",
    }


def test_run_verification_returns_the_job_outcomes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"items": [{"id": "vbj_1", "status": "succeeded"}]})

    jobs = _client(handler).run_verification(finding_id="fnd_1")

    assert [job["id"] for job in jobs] == ["vbj_1"]


def test_history_is_returned_whole() -> None:
    """The history is the authoritative record: the client must not trim it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "finding": {"id": "fnd_1"},
                "rebuttals": [{"id": "rbt_1"}],
                "verification_jobs": [{"id": "vbj_1"}],
                "dispositions": [{"id": "dsp_1"}],
            }
        )

    history = _client(handler).get_history("fnd_1")

    assert set(history) == {"finding", "rebuttals", "verification_jobs", "dispositions"}


# --- absence and pagination, against the shapes the real API returns ---------


def test_get_history_returns_none_when_the_finding_is_absent() -> None:
    """A missing history must read as "absent", not as a payload-shape failure.

    Found by driving the MCP server against a live NYX: `get_history` returned a body the
    caller could not distinguish from a broken response. The sentinel is part of the contract.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"code": "NOT_FOUND", "message": "finding does not exist"}, status=404
        )

    assert _client(handler).get_history("fnd_missing") is None


def test_list_critics_reads_the_critics_key() -> None:
    """`/critics` pages under ``critics``, not ``items`` -- feeding it to `_items` raised.

    This is the defect the live probe found: a healthy 200 response was reported as
    INVALID_OUTPUT, so `nyx_list_critics` was unusable end-to-end.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/api/v1/critics")
        return _json_response({"critics": [{"critic_id": "c001"}, {"critic_id": "c002"}]})

    critics = _client(handler).list_critics()

    assert [item["critic_id"] for item in critics] == ["c001", "c002"]


def test_list_critics_tolerates_an_empty_pack() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"critics": []})

    assert _client(handler).list_critics() == []


def test_empty_page_is_an_empty_list_not_a_shape_error() -> None:
    """A 200 with a null body is "nothing here", not a malformed payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(None)

    assert _client(handler).list_findings() == []


def test_empty_history_is_an_empty_dict_not_a_shape_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(None)

    assert _client(handler).get_history("fnd_1") is None
