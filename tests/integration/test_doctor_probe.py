"""H4: `nyx doctor --probe` reports real reachability without leaking secrets.

The probe is opt-in because a probe turns a third party being down into a failed check, and
`doctor` is run on every deployment. Without `--probe`, the semantics are exactly what they were.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nyx.cli.main import app


def _env(tmp_path: Path, database: Path, **overrides: str) -> dict[str, str]:
    """NYX settings for a CLI invocation, with the OS environment stripped of ``NYX_*``."""

    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(tmp_path)
    env.update(overrides)
    return env


def _checks(result) -> dict[str, dict[str, object]]:
    return {check["check"]: check for check in json.loads(result.output)["checks"]}


def _quiet_logging():
    """Raise the root logger's level for a CLI invocation.

    Alembic logs "Context impl SQLiteImpl." at INFO while ``doctor`` inspects migrations, and under
    pytest that line is routed through a handler whose stream may already be closed -- so the
    *handler* prints a "--- Logging error ---" traceback into the captured output and the test sees
    the CLI as having failed. Raising the root level means the record is never created. That is
    pytest's logging plumbing, not the CLI's stdout, and it is the same guard
    ``test_cli_contract.py`` uses.
    """

    import logging
    from contextlib import contextmanager

    @contextmanager
    def _guard():
        root = logging.getLogger()
        previous = root.level
        root.setLevel(logging.CRITICAL)
        try:
            yield
        finally:
            root.setLevel(previous)

    return _guard()


# --------------------------------------------------------------------------- the opt-in boundary


def test_probe_is_opt_in(tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``--probe`` no reachability check runs, so nothing external can fail the command."""

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    with _quiet_logging():
        plain = runner.invoke(app, ["doctor", "--json"], env=_env(tmp_path, database))
        probed = runner.invoke(app, ["doctor", "--probe", "--json"], env=_env(tmp_path, database))

    assert plain.exit_code == 0, plain.output
    assert not any(name.startswith(("llm_probe:", "adapter_probe")) for name in _checks(plain))

    assert probed.exit_code == 0, probed.output
    assert "llm_probe:llm_endpoint" in _checks(probed)


def test_probe_with_nothing_configured_passes_and_says_so(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe of nothing is not a failure; it is reported as a pass with the reason named."""

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    with _quiet_logging():
        result = runner.invoke(app, ["doctor", "--probe", "--json"], env=_env(tmp_path, database))

    checks = _checks(result)
    assert checks["llm_probe:llm_endpoint"]["status"] == "ok"
    assert "nothing to probe" in str(checks["llm_probe:llm_endpoint"]["message"])
    assert checks["adapter_probe"]["status"] == "ok"


# --------------------------------------------------------------------------- the LLM probe


def test_llm_probe_reports_an_unreachable_endpoint_as_a_failure(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who asks for a probe gets a failed check when the endpoint is down."""

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    env = _env(
        tmp_path,
        database,
        NYX_LLM_ENABLED="true",
        NYX_LLM_MODEL="probe-model",
        NYX_LLM_BASE_URL="http://127.0.0.1:59999",
        NYX_LLM_TIMEOUT_SECONDS="1",
    )

    with _quiet_logging():
        result = runner.invoke(app, ["doctor", "--probe", "--json"], env=env)

    assert result.exit_code == 1, result.output
    check = _checks(result)["llm_probe:llm_endpoint"]
    assert check["status"] == "fail"
    assert check["code"] == "DEPENDENCY_UNAVAILABLE"


def test_llm_probe_refuses_a_base_url_that_embeds_credentials(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe must not be the place a credential leaks, so a credential-bearing URL is refused.

    The password is assembled from parts rather than written as one literal, because the assertion
    searches the whole output for it -- and a test that embeds the value it forbids will always
    find its own source text.
    """

    secret = "hunter" + "2"
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    env = _env(
        tmp_path,
        database,
        NYX_LLM_ENABLED="true",
        NYX_LLM_MODEL="probe-model",
        NYX_LLM_BASE_URL=f"http://user:{secret}@example.invalid",
    )

    with _quiet_logging():
        result = runner.invoke(app, ["doctor", "--probe", "--json"], env=env)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    check = _checks(result)["llm_probe:llm_endpoint"]
    assert check["status"] == "fail"
    assert check["code"] == "INVALID_REQUEST"
    assert "Traceback" not in result.output
    # The diagnostic names the variable to fix, never the URL that carries the secret.
    assert secret not in result.output
    assert "NYX_LLM_BASE_URL" in str(check["message"])
    assert payload["checks"]


def test_llm_probe_reports_a_reachable_endpoint(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2xx from the models endpoint is a pass, and the credential is not in the output."""

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Models(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            if self.path != "/v1/models":
                self.send_error(404)
                return
            body = json.dumps({"object": "list", "data": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Models)
    port = server.server_address[1]
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        env = _env(
            tmp_path,
            database,
            NYX_LLM_ENABLED="true",
            NYX_LLM_MODEL="probe-model",
            NYX_LLM_BASE_URL=f"http://127.0.0.1:{port}",
            NYX_LLM_API_KEY="sk-probe-secret-value-0001",
        )

        with _quiet_logging():
            result = runner.invoke(app, ["doctor", "--probe", "--json"], env=env)

        assert result.exit_code == 0, result.output
        check = _checks(result)["llm_probe:llm_endpoint"]
        assert check["status"] == "ok"
        # The assertion that matters: the probe carried the key, and the output must not contain it.
        assert "sk-probe-secret-value-0001" not in result.output
    finally:
        server.shutdown()
        server.server_close()


def test_llm_probe_reports_a_4xx_as_a_failure_without_the_body(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error status is a failed check, and the response body is never echoed."""

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Unauthorized(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            body = b'{"error": {"message": "bad key sk-secret-in-body-0002"}}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Unauthorized)
    port = server.server_address[1]
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        env = _env(
            tmp_path,
            database,
            NYX_LLM_ENABLED="true",
            NYX_LLM_MODEL="probe-model",
            NYX_LLM_BASE_URL=f"http://127.0.0.1:{port}",
        )

        with _quiet_logging():
            result = runner.invoke(app, ["doctor", "--probe", "--json"], env=env)

        assert result.exit_code == 1, result.output
        check = _checks(result)["llm_probe:llm_endpoint"]
        assert check["status"] == "fail"
        assert "401" in str(check["message"])
        assert "sk-secret-in-body-0002" not in result.output
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- the adapter probe


def test_adapter_probe_probes_an_enabled_adapter(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enabled adapter is probed; the check is named after it."""

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    env = _env(
        tmp_path,
        database,
        NYX_ENABLED_ADAPTERS='["hermes"]',
        NYX_AGENT_TRANSCRIPT_DB=str(tmp_path / "absent-state.db"),
        NYX_AGENT_TRANSCRIPT_SYSTEM_ID="sys_probe",
    )

    with _quiet_logging():
        result = runner.invoke(app, ["doctor", "--probe", "--json"], env=env)

    assert result.exit_code == 1, result.output
    check = _checks(result)["adapter_probe:hermes"]
    assert check["status"] == "fail"
    assert check["code"] == "ADAPTER_UNAVAILABLE"
    # A missing transcript store is a degraded adapter, and the detail names it without a traceback.
    assert "Traceback" not in result.output


def test_adapter_probe_skips_a_disabled_adapter(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered-but-disabled adapter is not probed.

    It is not running, so there is nothing to reach, and probing one would construct a client the
    deployment deliberately switched off.
    """

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    env = _env(
        tmp_path,
        database,
        # No NYX_ENABLED_ADAPTERS at all: an unset list field is the empty set.
        # An empty *string* raises a SettingsError, so "unset" is how a test says "none".
        NYX_AGENT_TRANSCRIPT_DB=str(tmp_path / "absent-state.db"),
        NYX_AGENT_TRANSCRIPT_SYSTEM_ID="sys_probe",
    )

    with _quiet_logging():
        result = runner.invoke(app, ["doctor", "--probe", "--json"], env=env)

    assert result.exit_code == 0, result.output
    check = _checks(result)["adapter_probe"]
    assert check["status"] == "ok"
    assert "nothing to probe" in str(check["message"])
    assert not any(name.startswith("adapter_probe:") for name in _checks(result))


def test_probe_never_leaks_a_secret_from_effective_config(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe path is one more place a credential could appear; it must not."""

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    env = _env(
        tmp_path,
        database,
        NYX_LLM_ENABLED="true",
        NYX_LLM_MODEL="probe-model",
        NYX_LLM_API_KEY="sk-probe-leak-check-0003",
        NYX_LLM_BASE_URL="http://127.0.0.1:59998",
        NYX_LLM_TIMEOUT_SECONDS="1",
    )

    with _quiet_logging():
        result = runner.invoke(app, ["doctor", "--probe", "--json"], env=env)

    assert "sk-probe-leak-check-0003" not in result.output
    assert "***redacted***" in result.output


def test_probe_output_is_one_json_document(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI contract: `--json` emits exactly one document, on stdout, with nothing else."""

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    with _quiet_logging():
        result = runner.invoke(app, ["doctor", "--probe", "--json"], env=_env(tmp_path, database))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) is not None
