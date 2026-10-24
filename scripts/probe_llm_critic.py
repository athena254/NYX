"""Probe the wired model critic end to end, outside the pytest helpers.

Default (no ``--live``) starts a local OpenAI-shaped fixture server and needs no provider:

    uv run python scripts/probe_llm_critic.py

It migrates a throwaway SQLite database, seeds a synthetic completion claim, runs c011 through
the real registry with ``SyncCriticTransport``, and asserts the persisted ``critic_runs`` row
carries the provider, model, prompt-template version, token usage, and a finding that cites only
the evidence the response named.

``--live`` sends the same synthetic prompt to the configured provider (dmr-X by default) using
``NYX_LLM_*`` settings. It is an owner-gated probe, not a test: provider instability is reported
as a failure to obtain a structured assessment, not as a critic-quality claim.

Exit code 0 means the path worked; 1 means it did not. No secret is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sqlalchemy import select  # noqa: E402

from nyx.config import Settings  # noqa: E402
from nyx.critics.llm import CRITIC_ID  # noqa: E402
from nyx.critics.llm_transport import build_llm_critic  # noqa: E402
from nyx.critics.registry import run_selected  # noqa: E402
from nyx.ingestion.service import IngestionService  # noqa: E402
from nyx.llm.provider import LLMConfig  # noqa: E402
from nyx.storage.db import create_engine_from_settings, create_session_factory, session_scope  # noqa: E402
from nyx.storage.tables import CriticRunRow, FindingRow  # noqa: E402


def _migrate(directory: Path, database: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(directory)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")


def _seed(service: IngestionService) -> tuple[str, str]:
    """Persist a synthetic case; return ``(event_id, evidence_id)``."""

    system = service.register_system(
        {
            "name": "llm-critic-probe",
            "kind": "agent",
            "environment": "development",
            "owner": "probe",
            "adapter": "http",
        },
        request_id="probe-sys",
    )
    assert system.record_id is not None, system.error
    system_id = system.record_id

    claim = service.register_claim(
        {
            "statement": "All required tests pass for commit abc123.",
            "claimant_system_id": system_id,
            "claim_type": "completion",
            "scope": {"system_id": system_id, "artifact": "probe", "commit": "abc123"},
            "producer_event_id": "probe-claim-1",
        },
        request_id="probe-claim",
    )
    assert claim.record_id is not None, claim.error

    event = service.ingest_event(
        {
            "source_system_id": system_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-24T10:00:00Z",
            "correlation_id": "probe-corr-1",
            "payload": {"claim_id": claim.record_id},
            "producer_event_id": "probe-event-1",
            "trust_class": "observed",
            "labels": ["llm-critic-probe"],
            "retention_class": "standard",
        },
        request_id="probe-event",
    )
    assert event.record_id is not None, event.error

    evidence = service.register_evidence(
        {
            "kind": "ci_test_result",
            "title": "CI run for abc123",
            "description": "CI run for commit abc123: all checks passed.",
            "source_ref": "https://ci.example.invalid/run/probe",
            "source_system_id": system_id,
            "scope": {"system_id": system_id, "artifact": "probe", "commit": "abc123"},
            "captured_at": "2026-10-24T09:00:00Z",
            "valid_from": "2026-10-24T09:00:00Z",
            "integrity_method": "none",
            "relevance_to_claim": claim.record_id,
            "limitations": ["Synthetic probe evidence."],
            "sensitivity": "internal",
            "access_policy_ref": "probe",
            "retention_class": "standard",
            "producer_event_id": "probe-evidence-1",
        },
        request_id="probe-evidence-1",
    )
    assert evidence.record_id is not None, evidence.error
    return event.record_id, evidence.record_id


def _assessment(evidence_id: str) -> dict[str, Any]:
    return {
        "result": "finding",
        "proposition": "The linked CI evidence may not cover the claimed commit.",
        "rationale": "The probe response cites one linked evidence item as potentially mismatched.",
        "recommended_next_step": (
            "Ask an authorised human to compare the cited evidence with the claim."
        ),
        "observations": ["The probe reviewed the linked CI evidence."],
        "supporting_evidence_ids": [evidence_id],
        "counterevidence_ids": [],
        "limitations": ["Synthetic probe output; not verification."],
        "category": "other",
        "severity": "low",
        "title": "Probe-proposed evidence gap",
        "summary": "A model-proposed finding citing exactly one linked evidence item.",
    }


class _FixtureHandler(BaseHTTPRequestHandler):
    server_version = "llm-critic-probe"

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        plan: dict[str, Any] = self.server.plan  # type: ignore[attr-defined]
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(plan["assessment"]),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 21, "completion_tokens": 13},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


def _fixture_base_url(assessment: dict[str, Any]) -> tuple[str, ThreadingHTTPServer]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    httpd.plan = {"assessment": assessment}  # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


def _run(
    factory: Any,
    *,
    event_id: str,
    config: LLMConfig,
    provider: str,
) -> dict[str, Any]:
    from nyx.critics.registry import register

    register(build_llm_critic(config, provider=provider))
    started = time.monotonic()
    with session_scope(factory) as session:
        outcomes = run_selected(session, event_id=event_id, critic_ids=[CRITIC_ID])
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)

    with session_scope(factory) as session:
        run = session.scalars(select(CriticRunRow)).first()
        findings = list(session.scalars(select(FindingRow)))

    if run is None:
        raise SystemExit("no critic run was persisted")
    return {
        "outcome": outcomes[0].result.value,
        "finding_persisted": len(findings) == 1,
        "finding_status": findings[0].status if findings else None,
        "finding_evidence_ids": list(findings[0].evidence_ids) if findings else [],
        "model_provider": run.model_provider,
        "model_version": run.model_version,
        "prompt_version": run.prompt_version,
        "resource_usage": run.resource_usage,
        "prompt_logged": run.llm_io is not None,
        "elapsed_ms": elapsed_ms,
        "rationale": run.rationale,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="call the configured provider")
    parser.add_argument("--base-url", default=None, help="override NYX_LLM_BASE_URL")
    parser.add_argument("--model", default=None, help="override NYX_LLM_MODEL")
    parser.add_argument("--protocol", default=None, choices=["openai", "anthropic"])
    parser.add_argument("--timeout", type=float, default=None)
    args = parser.parse_args()

    settings = Settings()
    if args.live and not (args.model or settings.llm_model):
        print("NYX_LLM_MODEL is required for --live; the probe will not guess a model")
        return 1
    if settings.llm_critic_enabled or settings.llm_log_prompts:
        print(
            "The probe ignores NYX_LLM_CRITIC_ENABLED / NYX_LLM_LOG_PROMPTS; it enables c011 itself"
        )

    directory = Path(tempfile.mkdtemp(prefix="nyx-llm-critic-probe-"))
    database = directory / "probe.sqlite3"
    _migrate(directory, database)
    probe_settings = Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        data_dir=directory,
        redaction_enabled=True,
    )
    engine = create_engine_from_settings(probe_settings)
    factory = create_session_factory(engine)
    service = IngestionService(factory, probe_settings)
    event_id, evidence_id = _seed(service)

    httpd: ThreadingHTTPServer | None = None
    if args.live:
        config = LLMConfig(
            protocol=(args.protocol or settings.llm_protocol),
            base_url=args.base_url or settings.llm_base_url,
            api_key=(
                settings.llm_api_key.get_secret_value()
                if settings.llm_api_key is not None
                else None
            ),
            model=args.model or settings.llm_model or "",
            timeout_seconds=args.timeout or settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        provider = settings.llm_provider or config.protocol
        expected = "structured"
    else:
        base_url, httpd = _fixture_base_url(_assessment(evidence_id))
        config = LLMConfig(
            protocol="openai",
            base_url=base_url,
            model="fixture-model",
            timeout_seconds=args.timeout or 10.0,
        )
        provider = "fixture"
        expected = "finding"

    try:
        report = _run(factory, event_id=event_id, config=config, provider=provider)
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        engine.dispose()

    print(json.dumps(report, indent=2, default=str))

    if args.live:
        if report["outcome"] == "error":
            print("LIVE PROBE FAILED: no structured assessment was obtained")
            return 1
        print("LIVE PROBE OK: a structured assessment was obtained")
        return 0

    failures = []
    if report["outcome"] != expected:
        failures.append(f"outcome {report['outcome']!r} != {expected!r}")
    if not report["finding_persisted"]:
        failures.append("finding was not persisted")
    if report["finding_evidence_ids"] != [evidence_id]:
        failures.append("finding did not cite exactly the linked evidence")
    if report["model_provider"] != "fixture":
        failures.append("provider was not recorded")
    if report["model_version"] != "fixture-model":
        failures.append("model was not recorded")
    if not report["prompt_version"]:
        failures.append("prompt template version was not recorded")
    if report["resource_usage"] != {"input_tokens": 21.0, "output_tokens": 13.0}:
        failures.append(f"usage was not recorded: {report['resource_usage']!r}")
    if report["prompt_logged"]:
        failures.append("prompt logging was off but llm_io was stored")

    if failures:
        print("FIXTURE PROBE FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("FIXTURE PROBE OK: c011 ran through the registry and its provenance was persisted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
