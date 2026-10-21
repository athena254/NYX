"""Unit tests for the GitHub CI client and its configuration (phase 5, slice 5.1).

The property that matters most is that the token is handled correctly: it is read once from
the environment, placed in one ``Authorization`` header, and appears in no observable output.

Per ``docs/implementation/HANDOFF.md`` §7, the corrected-probe discipline is followed: the
synthetic token is put *only* in the input (an environment variable) and the assertions search
the outputs for that literal. The searched-for string is never built from the token, which
would report a leak the test itself caused.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nyx.adapters.git_ci import GitCiConfig, GitCiError, GitHubCiClient
from nyx.config import Settings

#: A public literal IP, so the SSRF guard's host check is deterministic and needs no DNS. A
#: name would make the positive tests depend on whatever the resolver answered today.
PUBLIC_BASE_URL = "http://93.184.216.34"
REPOSITORY = "octo/hello-world"
ENV_NAME = "NYX_TEST_GIT_CI_TOKEN"

#: A synthetic credential, used only as test input. It is not a real token and never leaves the
#: process.
SYNTHETIC_TOKEN = "ghp_syntheticSecretValue0000000000000000"  # noqa: S105 - test fixture


def _config(**overrides: Any) -> GitCiConfig:
    values: dict[str, Any] = {
        "provider": "github",
        "api_base_url": PUBLIC_BASE_URL,
        "repository": REPOSITORY,
        "token_env": None,
        "timeout_seconds": 10.0,
        "max_runs_per_fetch": 10,
        "allow_private_targets": False,
    }
    values.update(overrides)
    return GitCiConfig(**values)


class _Transport:
    """Records every request it carries and answers from one fixed payload or error."""

    def __init__(
        self,
        payload: object = None,
        *,
        status: int = 200,
        raw: httpx.Response | None = None,
    ) -> None:
        self.payload = payload
        self.status = status
        self.raw = raw
        self.error: Exception | None = None
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        return httpx.Response(self.status, json=self.payload)


def _client(transport: _Transport, **overrides: Any) -> GitHubCiClient:
    return GitHubCiClient(_config(**overrides), transport=httpx.MockTransport(transport))


# --------------------------------------------------------------------------- positive


def test_fetch_check_runs_returns_the_payload_and_makes_one_get(monkeypatch) -> None:
    monkeypatch.setenv(ENV_NAME, SYNTHETIC_TOKEN)
    payload = {
        "total_count": 2,
        "check_runs": [
            {"id": 1, "name": "build", "conclusion": "success"},
            {"id": 2, "name": "lint", "conclusion": "failure"},
        ],
    }
    transport = _Transport(payload)
    client = _client(transport, token_env=ENV_NAME)

    runs = client.fetch_check_runs(ref="main")

    assert runs == payload["check_runs"]
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/repos/octo/hello-world/commits/main/check-runs"
    # The header is the one place the token is allowed to be; asserting it here proves the
    # client really read the variable rather than silently sending no credentials.
    assert request.headers["authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
    assert client.last_truncated == 0


def test_fetch_actions_runs_requests_the_actions_endpoint() -> None:
    payload = {"total_count": 1, "workflow_runs": [{"id": 9, "name": "ci"}]}
    transport = _Transport(payload)
    client = _client(transport)

    runs = client.fetch_actions_runs(ref="main")

    assert runs == payload["workflow_runs"]
    assert transport.requests[0].url.path == "/repos/octo/hello-world/actions/runs"


# --------------------------------------------------------------------------- negatives


def test_http_401_is_unauthorized_and_carries_no_token(monkeypatch) -> None:
    monkeypatch.setenv(ENV_NAME, SYNTHETIC_TOKEN)
    transport = _Transport({"message": "Bad credentials"}, status=401)
    client = _client(transport, token_env=ENV_NAME)

    with pytest.raises(GitCiError) as excinfo:
        client.fetch_actions_runs(ref="main")

    assert excinfo.value.code == "unauthorized"
    assert SYNTHETIC_TOKEN not in str(excinfo.value)
    # The query string is stripped from the message: the ref rides in it, and a ref can be
    # sensitive even when the token is not.
    assert "head_sha" not in str(excinfo.value)
    assert "main" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_found"),
        (429, "unavailable"),
        (500, "unavailable"),
        (503, "unavailable"),
    ],
)
def test_http_status_maps_to_a_stable_code(status: int, code: str) -> None:
    transport = _Transport({"message": "boom"}, status=status)
    client = _client(transport)

    with pytest.raises(GitCiError) as excinfo:
        client.fetch_check_runs(ref="main")

    assert excinfo.value.code == code


@pytest.mark.parametrize(
    "error",
    [
        httpx.TimeoutException("deadline exceeded"),
        httpx.ConnectError("connection refused"),
    ],
)
def test_a_transport_failure_is_unavailable_and_never_raises(monkeypatch, error) -> None:
    monkeypatch.setenv(ENV_NAME, SYNTHETIC_TOKEN)
    transport = _Transport()
    transport.error = error
    client = _client(transport, token_env=ENV_NAME)

    with pytest.raises(GitCiError) as excinfo:
        client.fetch_check_runs(ref="main")

    assert excinfo.value.code == "unavailable"
    assert SYNTHETIC_TOKEN not in str(excinfo.value)


def test_a_non_json_body_is_invalid_output() -> None:
    """An HTML error page or a truncated body must not surface as a decode traceback."""

    transport = _Transport(raw=httpx.Response(200, content=b"<html>gateway error</html>"))
    client = _client(transport)

    with pytest.raises(GitCiError) as excinfo:
        client.fetch_check_runs(ref="main")

    assert excinfo.value.code == "invalid_output"


# ----------------------------------------------------------------- secret hygiene


def test_the_token_appears_in_no_observable_output(monkeypatch) -> None:
    """The one that matters: the token is in the header and nowhere else."""

    monkeypatch.setenv(ENV_NAME, SYNTHETIC_TOKEN)
    transport = _Transport({"message": "Bad credentials"}, status=401)
    client = _client(transport, token_env=ENV_NAME)

    with pytest.raises(GitCiError) as excinfo:
        client.fetch_check_runs(ref="main")

    # Present in the one place it belongs...
    assert transport.requests[0].headers["authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
    # ...and in none of the observable outputs.
    assert SYNTHETIC_TOKEN not in str(excinfo.value)
    assert SYNTHETIC_TOKEN not in repr(excinfo.value)
    assert SYNTHETIC_TOKEN not in repr(client)
    assert SYNTHETIC_TOKEN not in str(client)
    assert SYNTHETIC_TOKEN not in str(vars(client))
    assert SYNTHETIC_TOKEN not in repr(vars(client))
    assert SYNTHETIC_TOKEN not in str(list(vars(client).values()))
    assert SYNTHETIC_TOKEN not in repr(client._config)
    # The config holds the *name* of the variable, never the value.
    assert client._config.token_env == ENV_NAME


def test_a_private_target_is_refused_before_any_request(monkeypatch) -> None:
    monkeypatch.setenv(ENV_NAME, SYNTHETIC_TOKEN)
    transport = _Transport({"total_count": 0, "check_runs": []})
    client = _client(
        transport,
        api_base_url="http://127.0.0.1:8080",
        token_env=ENV_NAME,
    )

    with pytest.raises(GitCiError) as excinfo:
        client.fetch_check_runs(ref="main")

    assert excinfo.value.code == "denied"
    assert transport.requests == []


# --------------------------------------------------------------------- boundary


def test_max_runs_per_fetch_truncates_and_reports_it() -> None:
    runs = [{"id": index, "name": f"job-{index}"} for index in range(15)]
    transport = _Transport({"total_count": 15, "check_runs": runs, "workflow_runs": runs})
    client = _client(transport, max_runs_per_fetch=5)

    assert client.fetch_check_runs(ref="main") == runs[:5]
    assert client.last_truncated == 10
    assert client.fetch_actions_runs(ref="main") == runs[:5]
    assert client.last_truncated == 10

    # Nothing dropped is reported as 0, not left silent.
    small = _Transport({"check_runs": runs[:3]})
    exact = _client(small, max_runs_per_fetch=10)
    assert exact.fetch_check_runs(ref="main") == runs[:3]
    assert exact.last_truncated == 0


def test_from_settings_maps_the_git_ci_fields_and_never_the_token() -> None:
    settings = Settings(
        git_ci_provider="github",
        git_ci_api_base_url="https://api.github.com",
        git_ci_repository=REPOSITORY,
        git_ci_token_env=ENV_NAME,
        git_ci_timeout_seconds=7.5,
        git_ci_max_runs_per_fetch=3,
        git_ci_allow_private_targets=False,
    )

    config = GitCiConfig.from_settings(settings)

    assert config.provider == "github"
    assert config.api_base_url == "https://api.github.com"
    assert config.repository == REPOSITORY
    assert config.token_env == ENV_NAME
    assert config.timeout_seconds == 7.5
    assert config.max_runs_per_fetch == 3
    assert config.allow_private_targets is False
    # There is no field that could hold the value, so there is no field that could leak it.
    assert set(GitCiConfig.model_fields) == {
        "provider",
        "api_base_url",
        "repository",
        "token_env",
        "timeout_seconds",
        "max_runs_per_fetch",
        "allow_private_targets",
    }
    assert SYNTHETIC_TOKEN not in repr(config)
