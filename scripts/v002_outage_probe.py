"""Owned-relay outage probe for the optional model critic (v0.0.2 acceptance).

Task 11 asks for an outage induced through an relay the harness owns, never a shared provider.
This probe stands up a loopback relay, points NYX's model critic at it, and drives four
properties:

1. while the relay fails, the critic returns a bounded operational error rather than hanging or
   fabricating an assessment;
2. the failed run is recorded, so the failure is visible rather than silent;
3. when the relay recovers, a later run succeeds against the same configuration;
4. after the store is closed and reopened (a restart), both runs are still recorded.

Nothing here touches a real provider. The relay is in-process, on loopback, and stopped with the
probe. Output is one JSON receipt on stdout.
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

from nyx.config import Settings  # noqa: E402
from nyx.critics.registry import run_selected  # noqa: E402
from nyx.ingestion.service import IngestionService  # noqa: E402
from nyx.storage.db import (  # noqa: E402
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from nyx.storage.repositories.critic_runs import CriticRunRepository  # noqa: E402

ANSWER = {
    "result": "no_finding",
    "proposition": "The relay answered, so the assessment could proceed.",
    "rationale": "This is a synthetic relay response used to prove recovery, not a judgement.",
    "recommended_next_step": "No action; the run is an outage-recovery probe.",
    "observations": ["The relay returned a well-formed assessment."],
    "supporting_evidence_ids": [],
    "counterevidence_ids": [],
    "limitations": ["Synthetic relay response."],
}


class Relay:
    """A loopback OpenAI-compatible endpoint whose failures are counted."""

    def __init__(self) -> None:
        self.failures_remaining = 0
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own name
                outer.requests += 1
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                if outer.failures_remaining > 0:
                    outer.failures_remaining -= 1
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"relay induced outage"}')
                    return
                body = json.dumps(
                    {"choices": [{"message": {"role": "assistant", "content": json.dumps(ANSWER)}}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> Relay:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _migrate(directory: Path, database: Path, env: dict[str, str]) -> None:
    clean = {k: v for k, v in env.items() if not k.upper().startswith("NYX_")}
    clean["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    clean["NYX_DATA_DIR"] = str(directory)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env=clean,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"migration failed:\n{result.stdout}\n{result.stderr}")


def _settings(database: Path, directory: Path, relay: Relay) -> Settings:
    return Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        data_dir=directory,
        redaction_enabled=True,
        llm_critic_enabled=True,
        llm_model="outage-test-model",
        llm_protocol="openai",
        llm_base_url=f"http://127.0.0.1:{relay.port}/v1",
        llm_api_key=None,
        llm_timeout_seconds=5.0,
        llm_max_retries=0,
        llm_provider="owned-relay",
    )


def _seed(service: IngestionService, index: str) -> str:
    system = service.register_system(
        {
            "name": f"outage-{index}",
            "kind": "agent",
            "environment": "development",
            "owner": "outage-probe",
            "adapter": "http",
        },
        request_id=f"outage-{index}-sys",
    )
    assert system.record_id is not None, system.error
    system_id = system.record_id

    claim = service.register_claim(
        {
            "statement": "All required tests pass for commit abc123.",
            "claimant_system_id": system_id,
            "claim_type": "completion",
            "scope": {"system_id": system_id, "artifact": "demo", "commit": "abc123"},
            "producer_event_id": f"outage-{index}-claim",
        },
        request_id=f"outage-{index}-claim",
    )
    assert claim.record_id is not None, claim.error
    claim_id = claim.record_id

    event = service.ingest_event(
        {
            "source_system_id": system_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-24T10:00:00Z",
            "correlation_id": f"outage-{index}-corr",
            "payload": {"claim_id": claim_id},
            "producer_event_id": f"outage-{index}-event",
            "trust_class": "observed",
            "labels": ["outage-probe"],
            "retention_class": "standard",
        },
        request_id=f"outage-{index}-event",
    )
    assert event.record_id is not None, event.error
    return event.record_id


def _run_critic(factory: Any, event_id: str) -> dict[str, Any]:
    with session_scope(factory) as session:
        outcomes = run_selected(session, event_id=event_id, critic_ids=["c011-llm-critic"])
    outcome = outcomes[0]
    return {
        "result": outcome.result.value,
        "finding_id": outcome.finding_id,
        "rationale": (outcome.rationale or "")[:200],
    }


def _recorded_runs(factory: Any) -> list[dict[str, Any]]:
    with session_scope(factory) as session:
        runs = CriticRunRepository(session).list(limit=50)
        return [
            {"critic_id": run.critic_id, "result": run.result.value, "id": run.id} for run in runs
        ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    directory = Path(tempfile.mkdtemp(prefix="nyx-v002-outage-"))
    database = directory / "outage.sqlite3"

    with Relay() as relay:
        _migrate(directory, database, os.environ.copy())
        settings = _settings(database, directory, relay)

        engine = create_engine_from_settings(settings)
        factory = create_session_factory(engine)
        service = IngestionService(factory, settings)

        from nyx.critics import register_storage_backed
        from nyx.critics.llm_registration import register_llm_critic

        register_storage_backed(factory)
        assert register_llm_critic(settings), "the model critic did not register"

        event_id = _seed(service, "case")

        checks: dict[str, Any] = {}

        # 1. the relay is down: a bounded operational error, not a hang or a fabrication.
        relay.failures_remaining = 3
        started = time.monotonic()
        failed = _run_critic(factory, event_id)
        elapsed = round(time.monotonic() - started, 2)
        checks["bounded_failure"] = {
            "ok": failed["result"] == "error" and elapsed < 30.0,
            "result": failed["result"],
            "elapsed_seconds": elapsed,
        }

        # 2. the failure is recorded, so it is visible rather than silent.
        during = _recorded_runs(factory)
        checks["failure_recorded"] = {
            "ok": any(run["result"] == "error" for run in during),
            "runs": during,
        }

        # 3. the relay recovers: a later run succeeds against the same configuration.
        relay.failures_remaining = 0
        recovered = _run_critic(factory, event_id)
        checks["recovery"] = {
            "ok": recovered["result"] in {"no_finding", "finding", "inconclusive"},
            "result": recovered["result"],
            "rationale": recovered["rationale"],
        }

        # 4. the store is closed and reopened: both runs are still recorded.
        engine.dispose()
        engine = create_engine_from_settings(settings)
        factory = create_session_factory(engine)
        after = _recorded_runs(factory)
        results = {run["result"] for run in after}
        checks["durable_after_restart"] = {
            "ok": "error" in results and len(after) >= 2,
            "runs": after,
        }

        engine.dispose()

    receipt = {
        "relay_requests": relay.requests,
        "checks": checks,
        "ok": all(check["ok"] for check in checks.values()),
    }
    text = json.dumps(receipt, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
