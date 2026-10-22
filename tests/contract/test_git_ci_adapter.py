"""Phase 5 contract test for the Git/CI adapter (slice 5.3).

This is the contract SPEC §17.1 requires for "each adapter": it asserts the adapter's
*observable behaviour* against the boundary, not its internals. The shape is the
``scripts/probe_telegram_live.py`` pattern — a real socket, a real client, a fake *server* —
not a unit test with a mock object.

Three things make it a contract test rather than a unit test:

* **A real HTTP server on 127.0.0.1**, built with the standard library ``http.server`` in a
  thread. It speaks just enough of the GitHub API: ``/repos/{repo}``,
  ``/repos/{repo}/commits/{ref}/check-runs``, and ``/repos/{repo}/actions/runs``. The client is
  pointed at it through a real ``httpx.HTTPTransport`` — a real ``httpx.BaseTransport`` doing
  real socket I/O — so the adapter's requests are exercised end to end. **No network.**
* **The output is pushed through the real ``IngestionService``** against a migrated SQLite
  database from the ``session_factory`` fixture, so the test proves the adapter's output is
  acceptable to the boundary, not merely that it validates as a Pydantic model.
* **The secret-hygiene assertion (N7) is written outside the project's test helpers**: the
  token is placed *only* in an environment variable, and the assertion searches the outputs for
  that literal. The searched-for string is never built from the token
  (``docs/implementation/HANDOFF.md`` §7 records that exact mistake).

The cases P1–P10 and N1–N12 are the plan's §4 contract table
(``docs/implementation/phase-5-plan.md``).

One configuration note: the fake server is on ``127.0.0.1``, which the SSRF guard correctly
reports as a private target. A local GitHub Enterprise host or a test server is exactly the
deployment where an operator sets ``allow_private_targets=True``, so the positive tests do.
N6 asserts the guard still refuses — with **zero** requests recorded — when it is ``False``.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

import nyx.critics  # noqa: F401 - importing the package registers the deterministic critic pack
from nyx.adapters.base import Adapter, AdapterState
from nyx.adapters.git_ci.adapter import AdapterConfigurationError, GitCiAdapter
from nyx.adapters.git_ci.config import GitCiConfig
from nyx.adapters.git_ci.github_client import GitCiError, GitHubCiClient
from nyx.adapters.git_ci.normalize import NormalizationError
from nyx.config import Settings
from nyx.domain.enums import TrustClass
from nyx.findings.service import FindingQuery, FindingService
from nyx.ingestion.inputs import EventInput, EvidenceInput
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory

#: The repository the fake server speaks for.
REPOSITORY = "octo/demo"

#: The environment variable the client reads the PAT from. The *name* is configuration; the
#: value is never a setting.
TOKEN_ENV_NAME = "NYX_TEST_GIT_CI_CONTRACT_TOKEN"  # noqa: S105 - a variable name, not a secret

#: A synthetic credential, used only as test input. It is not a real token. It is a literal on
#: purpose: the assertion searches the outputs for this exact string, and building the
#: searched-for string from the token itself would report a leak the test caused.
SYNTHETIC_TOKEN = "ghp_synthetic_contract_token_0000000000"  # noqa: S105 - test fixture

#: The ``evidence:*`` read policies the evidence route accepts. The adapter must emit one of
#: these, never a free-form value.
KNOWN_ACCESS_POLICY_REFS = frozenset(
    {
        "evidence:read",
        "evidence:internal",
        "evidence:public",
        "evidence:standard",
        "evidence:restricted",
    }
)

#: C-001's own outcome markers. N4 asserts the adapter's text matches neither of them when the
#: host did not say pass or fail, so the assertion uses the critic's vocabulary rather than a
#: paraphrase of it.
_PASS_MARKER = re.compile(r"\bpass(?:es|ed|ing)?\b|\bgreen\b")
_FAIL_MARKER = re.compile(r"\bfail(?:s|ed|ing|ure)?\b|\bbroken\b|\bred\b")


# --------------------------------------------------------------------------- payloads


def _check_run(run_id: int, *, name: str, conclusion: str, head_sha: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": head_sha,
        "started_at": "2026-10-19T09:00:00Z",
        "completed_at": "2026-10-19T09:01:00Z",
        "check_suite": {"display_title": "required-integration"},
        "html_url": f"https://github.com/{REPOSITORY}/runs/{run_id}",
        "actor": {"login": "octocat"},
    }


def _actions_run(run_id: int, *, head_sha: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": f"workflow-{run_id}",
        "status": "completed",
        "conclusion": "success",
        "head_sha": head_sha,
        "run_started_at": "2026-10-19T09:00:00Z",
        "head_branch": "main",
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        "triggering_actor": {"login": "octocat"},
    }


#: Two check runs: one ``success`` on ``abc123``, one ``failure`` on ``def456``. The mismatch
#: between the two is what drives P9.
CHECK_RUNS: dict[str, Any] = {
    "total_count": 2,
    "check_runs": [
        _check_run(101, name="integration", conclusion="success", head_sha="abc123"),
        _check_run(102, name="audit", conclusion="failure", head_sha="def456"),
    ],
}

ACTIONS_RUNS: dict[str, Any] = {
    "total_count": 1,
    "workflow_runs": [_actions_run(201, head_sha="ghi789")],
}


# --------------------------------------------------------------------------- fake server


class _FakeGitHubHandler(BaseHTTPRequestHandler):
    """Speaks just enough of the GitHub API for the adapter's three GETs."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        server = self.server
        assert isinstance(server, _FakeGitHubServer)
        # Header names are lower-cased here so a test can look a name up without knowing how
        # the client capitalised it.
        server.requests.append(
            (self.command, self.path, {key.lower(): value for key, value in self.headers.items()})
        )
        status, body = server.route(self.path)
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        # A test server that logs to stderr on every request is noise, not signal.
        return


class _FakeGitHubServer(ThreadingHTTPServer):
    """A real HTTP server on 127.0.0.1 that records every request it is sent."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _FakeGitHubHandler)
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.routes: dict[str, tuple[int, object]] = {}
        self.default_status = 200
        self.default_body: object = {}

    def route(self, path: str) -> tuple[int, object]:
        for prefix, response in self.routes.items():
            if path.startswith(prefix):
                return response
        return self.default_status, self.default_body


class _HangingServer:
    """Accepts TCP connections and never answers, so a client's timeout is the only bound.

    N1 needs a host that is *reachable* but silent: a refused port proves the adapter reports
    ``DEGRADED`` quickly, but only a silent host proves it does not hang past ``timeout_seconds``.
    """

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(5)
        self.port = int(self._socket.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                self._socket.settimeout(0.2)
                connection, _ = self._socket.accept()
            except (TimeoutError, OSError):
                continue
            # Deliberately never read or write: the client's timeout is the only bound.
            _ = connection

    def close(self) -> None:
        self._stop.set()
        self._socket.close()
        self._thread.join(timeout=5)


# --------------------------------------------------------------------------- helpers


def _base_url(server: _FakeGitHubServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def _config(base_url: str, **overrides: Any) -> GitCiConfig:
    """Build an adapter config pointed at the fake server.

    ``allow_private_targets=True`` is required because the fake server is on ``127.0.0.1`` and
    the SSRF guard correctly refuses loopback. A local GitHub Enterprise host or a test server
    is exactly the deployment where an operator opts in; N6 asserts the guard still refuses when
    this is ``False``.
    """

    values: dict[str, Any] = {
        "provider": "github",
        "api_base_url": base_url,
        "repository": REPOSITORY,
        "token_env": TOKEN_ENV_NAME,
        "timeout_seconds": 5.0,
        "max_runs_per_fetch": 10,
        "allow_private_targets": True,
    }
    values.update(overrides)
    return GitCiConfig(**values)


def _default_routes() -> dict[str, tuple[int, object]]:
    return {
        f"/repos/{REPOSITORY}/commits/": (200, CHECK_RUNS),
        f"/repos/{REPOSITORY}/actions/runs": (200, ACTIONS_RUNS),
        f"/repos/{REPOSITORY}": (200, {"full_name": REPOSITORY}),
    }


def _closed_port() -> int:
    """Return a port that is free *now*, so connecting to it is refused."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _register_claim(service: IngestionService, commit: str) -> str:
    result = service.register_claim(
        {
            "statement": f"All required tests pass for commit {commit}.",
            "claimant_system_id": "sys_git_ci_demo",
            "claim_type": "completion",
            "scope": {
                "system_id": "sys_git_ci_demo",
                "artifact": "demo",
                "commit": commit,
            },
            "producer_event_id": f"contract-claim-{commit}",
        },
        request_id=f"contract-claim-{commit}",
    )
    assert result.status in {"created", "duplicate"}, result.error
    assert result.record_id is not None
    return result.record_id


def _adapter_evidence(adapter: GitCiAdapter, commit: str) -> EvidenceInput:
    """Return the adapter's evidence for one commit, from the standard check-runs payload."""

    for item in adapter.normalize(CHECK_RUNS):
        matches = (
            isinstance(item, EvidenceInput)
            and item.scope is not None
            and item.scope.commit == commit
        )
        if matches:
            return item
    raise AssertionError(f"the adapter produced no evidence for commit {commit!r}")


def _register_evidence(service: IngestionService, item: EvidenceInput, request_id: str) -> str:
    result = service.register_evidence(item.model_dump(mode="json"), request_id=request_id)
    assert result.status == "created", result.error
    assert result.record_id is not None
    return result.record_id


def _run_c001(
    session_factory: SessionFactory, service: IngestionService, claim_id: str, evidence_id: str
) -> tuple[str, str | None]:
    """Link one claim to one evidence item through an event, then run C-001 on it.

    The adapter's evidence is deliberately *unlinked* at registration (ADR 0008: the adapter
    observes a run before any claim references it). The link is established here, by the caller
    that files the claim — which is exactly the flow the ADR describes.
    """

    event = EventInput(
        source_system_id="git-ci",
        event_type="ci.check.completed",
        occurred_at="2026-10-19T09:30:00Z",
        correlation_id=f"contract-evt-{claim_id}-{evidence_id}",
        payload={"claim_id": claim_id, "evidence_ids": [evidence_id]},
        trust_class=TrustClass.OBSERVED,
        labels=[],
        retention_class="standard",
        producer_event_id=f"contract-evt-{claim_id}-{evidence_id}",
    )
    ingested = service.ingest_event(event, request_id="contract-event")
    assert ingested.status == "created", ingested.error

    outcomes = FindingService(session_factory).run_critics(
        ingested.record_id, ["c001-completion-vs-verification"]
    )
    c001 = next(item for item in outcomes if item.critic_id == "c001-completion-vs-verification")
    return c001.result.value, c001.finding_id


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def token_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """Put the synthetic token in the environment. It is input, never an assertion."""

    monkeypatch.setenv(TOKEN_ENV_NAME, SYNTHETIC_TOKEN)
    return TOKEN_ENV_NAME


@pytest.fixture()
def github_server() -> Iterator[_FakeGitHubServer]:
    server = _FakeGitHubServer(("127.0.0.1", 0))
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def adapter(github_server: _FakeGitHubServer, token_env: str) -> Iterator[GitCiAdapter]:
    github_server.routes = _default_routes()
    config = _config(_base_url(github_server), token_env=token_env)
    client = GitHubCiClient(config, transport=httpx.HTTPTransport())
    try:
        yield GitCiAdapter(config, client=client)
    finally:
        client.close()


# --------------------------------------------------------------------------- P1 – P10


def test_p1_the_adapter_satisfies_the_adapter_protocol(adapter: GitCiAdapter) -> None:
    """P1 — structural conformance, checked with ``runtime_checkable``."""

    assert isinstance(adapter, Adapter)
    assert adapter.adapter_id == "git-ci"
    assert adapter.version == "1.0.0"
    assert adapter.description
    assert adapter.permissions
    assert adapter.can_observe
    assert adapter.cannot_observe


def test_p2_normalize_output_is_accepted_by_the_real_ingestion_service(
    adapter: GitCiAdapter, service: IngestionService
) -> None:
    """P2 — the boundary accepts the adapter's output, not just its models."""

    inputs = adapter.normalize(CHECK_RUNS)

    events = [item for item in inputs if isinstance(item, EventInput)]
    evidence = [item for item in inputs if isinstance(item, EvidenceInput)]
    assert events, "the adapter produced no EventInput"
    assert evidence, "the adapter produced no EvidenceInput"

    for index, event in enumerate(events):
        result = service.ingest_event(event, request_id=f"p2-event-{index}")
        assert result.status == "created", result.error
    for index, item in enumerate(evidence):
        result = service.register_evidence(
            item.model_dump(mode="json"), request_id=f"p2-evidence-{index}"
        )
        assert result.status == "created", result.error


def test_p3_the_ingested_event_carries_the_runs_head_sha(
    adapter: GitCiAdapter, service: IngestionService
) -> None:
    """P3 — the event carries the head SHA C-001 correlates on."""

    event = next(item for item in adapter.normalize(CHECK_RUNS) if isinstance(item, EventInput))
    result = service.ingest_event(event, request_id="p3-event")
    assert result.status == "created", result.error

    stored = service.get_event(result.record_id)
    assert stored is not None
    assert stored.payload["commit"] == "abc123"


def test_p4_the_ingested_evidence_is_a_ci_test_result_for_the_same_commit(
    adapter: GitCiAdapter, service: IngestionService
) -> None:
    """P4 — the evidence is the record the critic actually compares."""

    item = _adapter_evidence(adapter, "abc123")
    result = service.register_evidence(item.model_dump(mode="json"), request_id="p4-evidence")
    assert result.status == "created", result.error

    stored = service.get_evidence(result.record_id)
    assert stored is not None
    assert stored.kind.value == "ci_test_result"
    assert stored.scope is not None
    assert stored.scope.commit == "abc123"


def test_p5_the_access_policy_ref_is_a_known_evidence_read_value(
    adapter: GitCiAdapter, service: IngestionService
) -> None:
    """P5 — a value the evidence route accepts, not a free-form string."""

    item = _adapter_evidence(adapter, "abc123")
    result = service.register_evidence(item.model_dump(mode="json"), request_id="p5-evidence")
    assert result.status == "created", result.error

    stored = service.get_evidence(result.record_id)
    assert stored is not None
    assert stored.access_policy_ref in KNOWN_ACCESS_POLICY_REFS


def test_p6_health_is_ok_against_the_fake_server_and_carries_no_credential(
    adapter: GitCiAdapter,
) -> None:
    """P6 — one bounded GET, ``OK``, and a detail free of any credential."""

    state = adapter.health()

    assert state.state is AdapterState.OK
    assert state.adapter_id == "git-ci"
    assert SYNTHETIC_TOKEN not in state.detail


def test_p7_fetch_for_ref_produces_stable_producer_event_ids(
    adapter: GitCiAdapter,
) -> None:
    """P7 — the identity key is a pure function of the external object."""

    first = adapter.fetch_for_ref("main")
    second = adapter.fetch_for_ref("main")

    assert first, "the adapter fetched nothing"
    assert [item.producer_event_id for item in first] == [item.producer_event_id for item in second]
    assert len({item.producer_event_id for item in first}) == len(first)


def test_p8_refetching_the_same_run_is_a_duplicate(
    adapter: GitCiAdapter, service: IngestionService
) -> None:
    """P8 — the adapter's identity key works end to end through the gateway."""

    event = next(item for item in adapter.fetch_for_ref("main") if isinstance(item, EventInput))

    first = service.ingest_event(event, request_id="p8-first")
    assert first.status == "created", first.error

    second = service.ingest_event(event, request_id="p8-second")
    assert second.status == "duplicate"
    assert second.error is not None
    assert second.error.code == "DUPLICATE_EVENT"
    assert second.record_id == first.record_id


def test_p9_adapter_evidence_for_a_different_commit_drives_c001(
    adapter: GitCiAdapter, service: IngestionService, session_factory: SessionFactory
) -> None:
    """P9 — the adapter's records are good enough to raise a real finding."""

    claim_id = _register_claim(service, "abc123")
    evidence_id = _register_evidence(service, _adapter_evidence(adapter, "def456"), "p9-evidence")

    result, finding_id = _run_c001(session_factory, service, claim_id, evidence_id)

    assert result == "finding"
    assert finding_id is not None

    finding = FindingService(session_factory).get_finding(finding_id)
    assert finding is not None
    # The references are the real persisted id, not an invented one.
    assert finding.supporting_evidence_ids == [evidence_id]
    assert finding.evidence_ids == [evidence_id]


def test_p10_same_commit_passing_evidence_produces_no_finding(
    adapter: GitCiAdapter, service: IngestionService, session_factory: SessionFactory
) -> None:
    """P10 — the negative case: a passing run on the claimed commit is not an accusation.

    C-001 reads the recorded outcome from the evidence title and description with
    word-boundary markers (``\\bpass(?:es|ed|ing)?\\b``, ``\\bgreen\\b``). The adapter states the
    host's own conclusion verbatim and never invents a pass/fail word, so a *passing* run
    reaches C-001 as passing only when the host's own job name says so. This run is named by
    the host as a passing job, and the adapter preserves that name.
    """

    payload = {
        "check_runs": [
            _check_run(103, name="integration-passed", conclusion="success", head_sha="abc123")
        ]
    }
    item = next(entry for entry in adapter.normalize(payload) if isinstance(entry, EvidenceInput))

    claim_id = _register_claim(service, "abc123")
    evidence_id = _register_evidence(service, item, "p10-evidence")

    result, finding_id = _run_c001(session_factory, service, claim_id, evidence_id)

    assert result == "no_finding"
    assert finding_id is None


# --------------------------------------------------------------------------- N1 – N12


def test_n1_health_against_an_unreachable_host_is_degraded_and_bounded(
    github_server: _FakeGitHubServer, token_env: str
) -> None:
    """N1 — unreachable is ``DEGRADED``, never a raise, and never an unbounded hang."""

    hanging = _HangingServer()
    try:
        config = _config(
            f"http://127.0.0.1:{hanging.port}", token_env=token_env, timeout_seconds=1.0
        )
        client = GitHubCiClient(config, transport=httpx.HTTPTransport())
        try:
            adapter = GitCiAdapter(config, client=client)
            started = time.monotonic()
            state = adapter.health()
            elapsed = time.monotonic() - started
        finally:
            client.close()
    finally:
        hanging.close()

    assert state.state is AdapterState.DEGRADED
    assert state.detail
    # The client's 1s timeout is the bound; a probe that ignored it would hang far longer.
    assert elapsed < 5.0, f"health() took {elapsed:.2f}s against a silent host"

    # A refused port is the same verdict, reached immediately.
    refused_config = _config(
        f"http://127.0.0.1:{_closed_port()}", token_env=token_env, timeout_seconds=1.0
    )
    refused_client = GitHubCiClient(refused_config, transport=httpx.HTTPTransport())
    try:
        refused = GitCiAdapter(refused_config, client=refused_client).health()
    finally:
        refused_client.close()

    assert refused.state is AdapterState.DEGRADED


def test_n2_a_payload_missing_the_head_sha_returns_no_records(
    adapter: GitCiAdapter,
) -> None:
    """N2 — no commit means no scope, so the record is skipped rather than emitted."""

    payload = {
        "check_runs": [
            {
                "id": 301,
                "name": "build",
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-10-19T09:00:00Z",
            }
        ]
    }

    assert adapter.normalize(payload) == []


def test_n3_a_payload_missing_timestamps_returns_no_records(
    adapter: GitCiAdapter,
) -> None:
    """N3 — a timestamp is required and is never defaulted to the fetch time."""

    payload = {
        "check_runs": [
            {
                "id": 302,
                "name": "build",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "abc123",
            }
        ]
    }
    actions_payload = {
        "workflow_runs": [
            {
                "id": 303,
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "abc123",
            }
        ]
    }

    assert adapter.normalize(payload) == []
    assert adapter.normalize(actions_payload) == []


def test_n4_an_unmapped_conclusion_is_preserved_without_an_invented_outcome(
    adapter: GitCiAdapter,
) -> None:
    """N4 — the host's own conclusion, verbatim, and no pass/fail word it did not use."""

    payload = {
        "check_runs": [_check_run(304, name="build", conclusion="neutral", head_sha="abc123")]
    }

    inputs = adapter.normalize(payload)
    assert inputs

    item = next(entry for entry in inputs if isinstance(entry, EvidenceInput))
    text = f"{item.title} {item.description}"
    assert "neutral" in item.description
    assert not _PASS_MARKER.search(text), f"the adapter invented a pass word: {text!r}"
    assert not _FAIL_MARKER.search(text), f"the adapter invented a fail word: {text!r}"


@pytest.mark.parametrize(
    "payload",
    [
        # A required identity field is missing.
        {"check_runs": [{"name": "build", "status": "completed", "conclusion": "success"}]},
        # A required field is the wrong type.
        {
            "check_runs": [
                {
                    "id": 305,
                    "name": 123,
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": "abc123",
                    "completed_at": "2026-10-19T09:00:00Z",
                }
            ]
        },
        # The runs list is the wrong type.
        {"check_runs": "not-a-list"},
        # The payload is not an object at all.
        ["not", "an", "object"],
    ],
)
def test_n5_a_malformed_payload_raises_a_typed_error_naming_the_field(
    adapter: GitCiAdapter, payload: object
) -> None:
    """N5 — a structured refusal, never a ``KeyError``/``AttributeError`` traceback."""

    with pytest.raises(NormalizationError) as excinfo:
        adapter.normalize(payload)

    assert excinfo.value.field
    assert not isinstance(excinfo.value, KeyError)
    assert not isinstance(excinfo.value, AttributeError)


def test_n6_a_private_target_is_refused_before_any_request(
    github_server: _FakeGitHubServer, token_env: str
) -> None:
    """N6 — the SSRF guard refuses loopback before a request exists to record."""

    config = _config(_base_url(github_server), token_env=token_env, allow_private_targets=False)
    client = GitHubCiClient(config, transport=httpx.HTTPTransport())
    try:
        adapter = GitCiAdapter(config, client=client)
        with pytest.raises(GitCiError) as excinfo:
            adapter.fetch_for_ref("main")
        assert excinfo.value.code == "denied"
    finally:
        client.close()

    assert github_server.requests == []


def test_n7_the_token_appears_in_no_observable_output(
    adapter: GitCiAdapter, github_server: _FakeGitHubServer, token_env: str
) -> None:
    """N7 — the token is in the header, and in nothing the adapter reports."""

    github_server.routes = {}
    github_server.default_status = 401
    github_server.default_body = {"message": "Bad credentials"}

    with pytest.raises(GitCiError) as excinfo:
        adapter.fetch_for_ref("main")

    state = adapter.health()
    settings = Settings(git_ci_token_env=token_env)

    observed = [
        str(excinfo.value),
        repr(excinfo.value),
        repr(adapter.client),
        str(adapter.client),
        state.detail,
        repr(adapter.config),
        adapter.config.model_dump_json(),
        settings.display_effective_config(),
    ]
    for output in observed:
        assert SYNTHETIC_TOKEN not in output, f"a credential leaked into: {output!r}"

    # Present in the one place it belongs: the Authorization header the client builds.
    headers = [headers for _, _, headers in github_server.requests if "authorization" in headers]
    assert headers, "the client sent no Authorization header"
    assert all(header["authorization"] == f"Bearer {SYNTHETIC_TOKEN}" for header in headers)


def test_n8_an_http_401_is_unauthorized_and_health_is_degraded(
    adapter: GitCiAdapter, github_server: _FakeGitHubServer
) -> None:
    """N8 — an auth failure is an error, never a silent "no runs"."""

    github_server.routes = {}
    github_server.default_status = 401
    github_server.default_body = {"message": "Bad credentials"}

    with pytest.raises(GitCiError) as excinfo:
        adapter.fetch_for_ref("main")
    assert excinfo.value.code == "unauthorized"

    state = adapter.health()
    assert state.state is AdapterState.DEGRADED


def test_n9_a_response_larger_than_the_limit_is_truncated_and_reported(
    github_server: _FakeGitHubServer, token_env: str
) -> None:
    """N9 — a bounded fetch that reports what it dropped."""

    runs = [
        _check_run(
            1000 + index,
            name=f"job-{index}",
            conclusion="success",
            head_sha=f"sha{index:04d}",
        )
        for index in range(15)
    ]
    actions = [_actions_run(2000 + index, head_sha=f"sha{index:04d}") for index in range(15)]
    github_server.routes = {
        f"/repos/{REPOSITORY}/commits/": (200, {"total_count": 15, "check_runs": runs}),
        f"/repos/{REPOSITORY}/actions/runs": (200, {"total_count": 15, "workflow_runs": actions}),
        f"/repos/{REPOSITORY}": (200, {"full_name": REPOSITORY}),
    }

    config = _config(_base_url(github_server), token_env=token_env, max_runs_per_fetch=5)
    client = GitHubCiClient(config, transport=httpx.HTTPTransport())
    try:
        adapter = GitCiAdapter(config, client=client)
        inputs = adapter.fetch_for_ref("main")
    finally:
        client.close()

    events = [item for item in inputs if isinstance(item, EventInput)]
    evidence = [item for item in inputs if isinstance(item, EvidenceInput)]
    # Five check runs plus five Actions runs, each yielding an event and an evidence item.
    assert len(events) == 10
    assert len(evidence) == 10
    # The truncation is reported, not silent.
    assert adapter.client.last_truncated == 10


def test_n10_fetch_for_ref_with_no_repository_raises_a_typed_configuration_error(
    github_server: _FakeGitHubServer, token_env: str
) -> None:
    """N10 — a misconfiguration is a typed error, not a ``TypeError`` on ``None``."""

    # ``GitCiConfig``'s pattern refuses an empty repository, so the misconfiguration is built
    # with ``model_construct`` — the validator is exactly what a real deployment would hit
    # first. The adapter must still refuse it *itself*, at the point of the call.
    config = GitCiConfig.model_construct(
        provider="github",
        api_base_url=_base_url(github_server),
        repository="",
        token_env=token_env,
        timeout_seconds=5.0,
        max_runs_per_fetch=10,
        allow_private_targets=True,
    )
    client = GitHubCiClient(config, transport=httpx.HTTPTransport())
    try:
        adapter = GitCiAdapter(config, client=client)
        with pytest.raises(AdapterConfigurationError) as excinfo:
            adapter.fetch_for_ref("main")
    finally:
        client.close()

    assert excinfo.value.field == "repository"
    assert not isinstance(excinfo.value, TypeError)
    assert github_server.requests == []


def test_n11_the_adapter_performs_only_get_requests(
    adapter: GitCiAdapter, github_server: _FakeGitHubServer
) -> None:
    """N11 — the method allowlist: a read-only observer issues no write."""

    adapter.fetch_for_ref("main")
    adapter.health()

    methods = {method for method, _, _ in github_server.requests}
    assert methods == {"GET"}
    assert github_server.requests, "the adapter made no requests at all"


def test_n12_a_full_fetch_and_ingest_leaves_the_findings_table_empty(
    adapter: GitCiAdapter, service: IngestionService, session_factory: SessionFactory
) -> None:
    """N12 — the adapter produces inputs; it never composes a finding."""

    for item in adapter.fetch_for_ref("main"):
        if isinstance(item, EventInput):
            result = service.ingest_event(item, request_id="n12-event")
        elif isinstance(item, EvidenceInput):
            result = service.register_evidence(
                item.model_dump(mode="json"), request_id="n12-evidence"
            )
        else:
            raise AssertionError(f"unexpected adapter output: {type(item).__name__}")
        assert result.status == "created", result.error

    page = FindingService(session_factory).list_findings(FindingQuery())
    assert page.items == []
