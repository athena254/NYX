"""The NYX MCP server: the tool surface is the safety property.

The claim this file exists to test is narrow and load-bearing: **the server exposes no tool that can
create a finding, record a disposition, or change a lifecycle state.** If that ever regressed, a
language model driving this server could produce a verdict -- and NYX's whole value is that its
verdicts are deterministic and evidence-linked.

The second claim is that NYX's refusals arrive as reportable results rather than exceptions, so a
model sees "NYX refused, here is why" instead of a stack trace it might be tempted to work around.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from nyx.conversational import mcp_server
from nyx.conversational.client import NyxClient
from nyx.conversational.mcp_server import FORBIDDEN_TOOLS, build_server


def _server(handler) -> MCPServer:
    return build_server(NyxClient("http://nyx.test", transport=httpx.MockTransport(handler)))


def _ok(payload: object = {"items": []}) -> httpx.Response:
    """An empty page: what the live API returns for an empty store."""

    return httpx.Response(200, json=payload)


def test_every_tool_is_registered() -> None:
    tools = asyncio.run(_server(_ok).list_tools())
    names = {tool.name for tool in tools}

    assert names == set(mcp_server.tool_names())
    assert len(names) == 12


@pytest.mark.parametrize("forbidden", FORBIDDEN_TOOLS)
def test_no_tool_can_act_as_a_judge(forbidden: str) -> None:
    """A model driving this server cannot produce a verdict.

    Each of these would let it create a finding or decide an outcome. None may ever be registered.
    """

    tools = asyncio.run(_server(_ok).list_tools())

    assert forbidden not in {tool.name for tool in tools}


def test_the_only_writes_offered_are_rebuttal_and_verification() -> None:
    tools = asyncio.run(_server(_ok).list_tools())
    names = {tool.name for tool in tools}
    writes = {name for name in names if name.startswith("nyx_submit") or "verification" in name}

    assert writes == {
        "nyx_submit_rebuttal",
        "nyx_request_verification",
        "nyx_run_verification",
        "nyx_list_verification_jobs",
        "nyx_get_verification_job",
    }


def test_read_tool_returns_nyx_data_as_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"items": [{"id": "fnd_1", "status": "detected"}]})

    result = asyncio.run(_server(handler).call_tool("nyx_list_findings", {"limit": 10}))

    payload = json.loads(_text(result))
    assert payload["operation"] == "list_findings"
    assert [item["id"] for item in payload["results"]] == ["fnd_1"]


def test_write_tool_reaches_nyx_and_returns_the_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"id": "rbt_1", "finding_id": "fnd_1"})

    result = asyncio.run(
        _server(handler).call_tool(
            "nyx_submit_rebuttal",
            {
                "finding_id": "fnd_1",
                "submitted_by": "sys_ghost",
                "argument": "A challenge.",
            },
        )
    )

    payload = json.loads(_text(result))
    assert payload["operation"] == "submit_rebuttal"
    assert payload["result"]["id"] == "rbt_1"


def test_tool_result_shape_does_not_depend_on_the_store_being_empty() -> None:
    """An empty result and a populated one must parse the same way.

    The tool layer used to wrap only the empty case, so a model had to branch on whether the
    store happened to hold anything. Both shapes are now identical.
    """

    empty = asyncio.run(
        _server(lambda _request: _ok({"items": []})).call_tool("nyx_list_findings", {})
    )
    populated = asyncio.run(
        _server(lambda _request: _ok({"items": [{"id": "fnd_1"}]})).call_tool(
            "nyx_list_findings", {}
        )
    )

    assert json.loads(_text(empty))["results"] == []
    assert [item["id"] for item in json.loads(_text(populated))["results"]] == ["fnd_1"]


def test_nyx_refusal_is_a_result_not_an_exception() -> None:
    """A model must see that NYX refused, with the reason -- never a stack trace."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "code": "CAPABILITY_REQUIRED",
                "message": "principal lacks capability 'findings.disposition'",
                "request_id": "r1",
            },
        )

    result = asyncio.run(_server(handler).call_tool("nyx_get_history", {"finding_id": "fnd_1"}))

    payload = json.loads(_text(result))
    assert payload["error"] == "CAPABILITY_REQUIRED"
    assert "findings.disposition" in payload["message"]
    assert payload["operation"] == "get_history"


def test_unavailable_nyx_is_reported_rather_than_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = asyncio.run(_server(handler).call_tool("nyx_list_findings", {}))

    payload = json.loads(_text(result))
    assert payload["error"] == "UNAVAILABLE"


def test_empty_results_are_explicit_not_an_empty_string() -> None:
    """A tool result must never be an empty string.

    Found by driving the server against a live NYX: a legitimate empty result serialised to
    `""`, which a model cannot tell apart from a failed call. The shape now says which it is.
    """

    result = asyncio.run(
        _server(lambda _request: _ok({"items": []})).call_tool("nyx_list_findings", {})
    )

    payload = json.loads(_text(result))
    assert payload["operation"] == "list_findings"
    assert payload["results"] == []


def test_absent_record_is_an_explicit_null_not_an_empty_string() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "Finding not found."})

    result = asyncio.run(
        _server(handler).call_tool("nyx_get_finding", {"finding_id": "fnd_missing"})
    )

    payload = json.loads(_text(result))
    assert payload["operation"] == "get_finding"
    assert payload["result"] is None
    assert "error" not in payload


def test_server_instructions_forbid_inventing_verdicts() -> None:
    """The prompt is part of the safety surface: it must not be possible to paraphrase a verdict."""

    tools = asyncio.run(_server(_ok).list_tools())
    assert tools, "the server registered no tools"

    instructions = _server(_ok).instructions or ""
    assert "MUST NOT" in instructions
    assert "untrusted data" in instructions


def _text(result: object) -> str:
    """Extract text from a CallToolResult without depending on its exact shape."""

    blocks = getattr(result, "content", None)
    if isinstance(blocks, list) and blocks:
        first = blocks[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            return text
    if isinstance(result, str):
        return result
    raise AssertionError(f"could not extract text from {type(result).__name__}")
