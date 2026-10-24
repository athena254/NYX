"""One wire shape: the async chat client and the sync critic transport send the same request.

The provider code moved to ``nyx.llm`` in Phase 6 so both call styles could share
``encode_request``/``parse_response``. This test is the anti-drift gate: a real socket server
records the request each style makes, and the two must agree on method, path, headers, and body.

Nothing here contacts a provider. The server speaks only the two protocol shapes NYX sends.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from nyx.critics.llm import OUTPUT_SCHEMA
from nyx.critics.llm_transport import SyncCriticTransport
from nyx.llm.provider import ChatMessage, LLMConfig, build_client

_PROMPT = "Assess the quoted evidence and return JSON."
_KEY = "sk-parity-distinctive-key-42"

_OPENAI_BODY: dict[str, Any] = {
    "choices": [{"message": {"role": "assistant", "content": '{"result": "no_finding"}'}}]
}
_ANTHROPIC_BODY: dict[str, Any] = {
    "content": [{"type": "text", "text": '{"result": "no_finding"}'}]
}


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: dict[str, Any] = field(default_factory=dict)


class _Recorder(BaseHTTPRequestHandler):
    server_version = "parity-test"

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        body = json.loads(raw)
        recorder = self.server.requests  # type: ignore[attr-defined]
        recorder.append(
            Recorded(
                method=self.command,
                path=self.path,
                headers={key.lower(): value for key, value in self.headers.items()},
                body=body,
            )
        )
        payload = _ANTHROPIC_BODY if self.path.endswith("/v1/messages") else _OPENAI_BODY
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture()
def server() -> Iterator[ThreadingHTTPServer]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    httpd.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _config(protocol: str, port: int) -> LLMConfig:
    return LLMConfig(
        protocol=protocol,  # type: ignore[arg-type]
        base_url=f"http://127.0.0.1:{port}",
        api_key=_KEY,
        timeout_seconds=5.0,
    )


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_both_call_styles_send_an_identical_request(
    server: ThreadingHTTPServer, protocol: str
) -> None:
    config = _config(protocol, server.server_address[1])  # type: ignore[attr-defined]

    client = build_client(config)
    asyncio.run(client.chat([ChatMessage(role="user", content=_PROMPT)]))

    transport = SyncCriticTransport(config)
    transport.complete(prompt=_PROMPT, schema=OUTPUT_SCHEMA, timeout_seconds=30.0)

    requests: list[Recorded] = server.requests  # type: ignore[attr-defined]
    assert len(requests) == 2
    async_request, sync_request = requests

    assert async_request.method == sync_request.method == "POST"
    assert async_request.path == sync_request.path
    expected_path = "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
    assert async_request.path == expected_path
    assert async_request.body == sync_request.body

    # The headers that carry protocol meaning must agree exactly.
    assert async_request.headers["content-type"] == sync_request.headers["content-type"]
    if protocol == "anthropic":
        assert async_request.headers["x-api-key"] == sync_request.headers["x-api-key"]
        assert (
            async_request.headers["anthropic-version"] == sync_request.headers["anthropic-version"]
        )
        assert "authorization" not in sync_request.headers
    else:
        assert async_request.headers["authorization"] == sync_request.headers["authorization"]
        assert "x-api-key" not in sync_request.headers

    # The prompt is the sole user message in both styles, and no schema is smuggled onto the wire.
    assert async_request.body["messages"] == [{"role": "user", "content": _PROMPT}]


def test_the_sync_transport_accepts_an_injected_transport() -> None:
    """Shape used by tests and by the parity check: no real network required."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OPENAI_BODY)

    transport = SyncCriticTransport(_config("openai", 3000), transport=httpx.MockTransport(handler))
    assert (
        transport.complete(prompt=_PROMPT, schema=OUTPUT_SCHEMA, timeout_seconds=30.0)
        == '{"result": "no_finding"}'
    )
