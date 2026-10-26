"""`nyx golden generate` must report the failures it counts.

A command that prints "3 failed" and nothing else reports a failure it cannot explain: the
operator learns that three clauses produced nothing, but not which clauses, and not why. This
test pins the reporting shape in both output modes, using a fake transport so nothing here
touches a provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nyx.cli.main import app

runner = CliRunner()


def _draft_response() -> str:
    """A valid draft response. There is no ``case_id``: the id is derived from the clause.

    The scope is a real one: ``system_id`` must be the ``@system`` placeholder and the scope
    must narrow beyond the system, or the generator refuses the case.
    """

    return json.dumps(
        {
            "title": "Disposition without a reviewer",
            "claim": {
                "statement": "All tests pass for commit abc123.",
                "claim_type": "completion",
                "scope": {"system_id": "@system", "artifact": "demo", "commit": "abc123"},
            },
            "event": {"event_type": "agent.completion_claim", "payload": {}},
            "evidence": [
                {
                    "kind": "ci_test_result",
                    "title": "CI run",
                    "description": "CI run for commit abc123: all checks passed.",
                }
            ],
        }
    )


class _FlakyTransport:
    """Answers the first clause and fails the second, so one run has both outcomes."""

    def __init__(self, response: str, error: Exception) -> None:
        self.response = response
        self.error = error
        self.prompts: list[str] = []

    def complete(self, *, prompt: str, schema: dict, timeout_seconds: float) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return self.response
        raise self.error


def _enable_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the LLM settings in the environment, so the test does not depend on ``.env``."""

    monkeypatch.setenv("NYX_LLM_ENABLED", "true")
    monkeypatch.setenv("NYX_LLM_MODEL", "test-model")
    monkeypatch.setenv("NYX_LLM_BASE_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("NYX_LLM_TIMEOUT_SECONDS", "5")


def _two_clause_spec(path: Path) -> Path:
    path.write_text(
        "# Spec\n\n## Section\n\n"
        "- A system must not record a disposition without a named reviewer.\n"
        "- Evidence whose validity window has closed must be reported as stale.\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def flaky_transport(monkeypatch: pytest.MonkeyPatch) -> _FlakyTransport:
    """Replace the CLI's transport construction with one that fails the second clause."""

    _enable_llm(monkeypatch)
    transport = _FlakyTransport(_draft_response(), RuntimeError("provider said no"))
    monkeypatch.setattr("nyx.cli.golden.SyncCriticTransport", lambda config: transport)
    return transport


def test_a_failed_clause_is_named_with_its_reason(
    tmp_path: Path, flaky_transport: _FlakyTransport
) -> None:
    spec = _two_clause_spec(tmp_path / "spec.md")
    log = tmp_path / "cases.jsonl"

    result = runner.invoke(
        app, ["golden", "generate", "--spec", str(spec), "--log", str(log), "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["drafted"] == 1
    assert payload["failed"] == 1

    failure = payload["failures"][0]
    assert "validity window has closed" in failure["clause"]
    assert "provider said no" in failure["error"]


def test_human_output_prints_the_failure(tmp_path: Path, flaky_transport: _FlakyTransport) -> None:
    spec = _two_clause_spec(tmp_path / "spec.md")
    log = tmp_path / "cases.jsonl"

    result = runner.invoke(app, ["golden", "generate", "--spec", str(spec), "--log", str(log)])

    assert result.exit_code == 0, result.output
    assert "FAILED" in result.output
    assert "validity window has closed" in result.output
    assert "provider said no" in result.output


class _ReliableTransport:
    """Answers every clause, so a run has no failures to report."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, *, prompt: str, schema: dict, timeout_seconds: float) -> str:
        self.prompts.append(prompt)
        return self.response


def test_a_successful_run_reports_no_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_llm(monkeypatch)
    monkeypatch.setattr(
        "nyx.cli.golden.SyncCriticTransport", lambda config: _ReliableTransport(_draft_response())
    )

    spec = _two_clause_spec(tmp_path / "spec.md")
    log = tmp_path / "cases.jsonl"

    result = runner.invoke(
        app, ["golden", "generate", "--spec", str(spec), "--log", str(log), "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["failures"] == []


def test_the_drafted_case_is_logged_with_the_derived_id(
    tmp_path: Path, flaky_transport: _FlakyTransport
) -> None:
    """The log carries the id derived from the clause, which is unique per clause."""

    spec = _two_clause_spec(tmp_path / "spec.md")
    log = tmp_path / "cases.jsonl"

    result = runner.invoke(app, ["golden", "generate", "--spec", str(spec), "--log", str(log)])

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    ids = [event["case_id"] for event in events]
    assert len(ids) == len(set(ids)), "two clauses must not share a case id"
    assert any("disposition" in case_id for case_id in ids)
