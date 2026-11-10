"""Acceptance harness for the NYX v0.0.2 request-level corpus and restart journey.

Where ``scripts/eval_v002_corpus.py`` scores the deterministic critics against the corpus, this
harness drives the cases that only exist across a process boundary: authentication, derived-output
masking, citation validation, the audit chain, the durable assessment queue, and the restart
journey from claim to authorised disposition.

It talks to a real uvicorn server over real HTTP, on a loopback port it owns, against a database it
creates. It never touches a shared provider and never stops a process it did not start. The only
provider call it makes is to NYX itself.

Output is one JSON receipt on stdout, written to ``--out`` when given.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
CORPUS = REPO / "evals" / "v0.0.2-corpus" / "cases.jsonl"

OPERATOR_KEY = "v002-operator-key"
READER_KEY = "v002-reader-key"
MARKER = "hunter2-super-secret"

_OPERATOR_CAPABILITIES = [
    "systems.write",
    "systems.read",
    "events.write",
    "events.read",
    "claims.write",
    "claims.read",
    "evidence.write",
    "evidence.read",
    "findings.read",
    "critics.read",
    "critics.run",
    "findings.rebut",
    "verification.request",
    "verification.run",
    "verification.read",
    "findings.disposition",
    "policies.read",
]
_READER_CAPABILITIES = [
    "systems.read",
    "events.read",
    "claims.read",
    "evidence.read",
    "findings.read",
    "critics.read",
    "verification.read",
    "policies.read",
]


class Harness:
    def __init__(self, port: int, workspace: Path) -> None:
        self.port = port
        self.workspace = workspace
        self.database = workspace / "acceptance.sqlite3"
        self.checkpoint = workspace / "audit-checkpoint.jsonl"
        self.base = f"http://127.0.0.1:{port}/api/v1"
        self.server: subprocess.Popen[bytes] | None = None
        self.client = httpx.Client(timeout=30.0)
        self.results: list[dict[str, Any]] = []

    # --- process lifecycle -------------------------------------------------

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "NYX_DATABASE_URL": f"sqlite:///{self.database.as_posix()}",
                "NYX_DATA_DIR": str(self.workspace),
                "NYX_AUTH_MODE": "api_key",
                "NYX_AUTH_PRINCIPALS": json.dumps(
                    [
                        {
                            "principal_id": "operator",
                            "api_key_env": "NYX_KEY_OPERATOR",
                            "capabilities": _OPERATOR_CAPABILITIES,
                        },
                        {
                            "principal_id": "reader",
                            "api_key_env": "NYX_KEY_READER",
                            "capabilities": _READER_CAPABILITIES,
                        },
                    ]
                ),
                "NYX_KEY_OPERATOR": OPERATOR_KEY,
                "NYX_KEY_READER": READER_KEY,
            }
        )
        return env

    def migrate(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO,
            env=self.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SystemExit(f"migration failed:\n{result.stdout}\n{result.stderr}")

    def start(self) -> None:
        self.server = subprocess.Popen(
            [
                str(VENV_PYTHON),
                "-m",
                "uvicorn",
                "--factory",
                "nyx.api.app:create_app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            env=self.environment(),
            cwd=str(REPO),
            stdout=(self.workspace / "server.log").open("w"),
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.server.poll() is not None:
                raise SystemExit("server exited during startup")
            try:
                if self.client.get(f"{self.base}/health", timeout=1.0).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise SystemExit("server did not become ready")

    def stop(self) -> None:
        if self.server is None:
            return
        self.server.terminate()
        try:
            self.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=10)
        self.server = None

    # --- helpers -----------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        key: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any], str]:
        headers = {}
        if key is not None:
            headers["X-API-Key"] = key
        response = self.client.request(
            method, f"{self.base}{path}", headers=headers, json=body or {}
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload, response.text

    def record(
        self, case_id: str, expected: str, ok: bool, detail: str, *, skip: bool = False
    ) -> None:
        self.results.append(
            {
                "id": case_id,
                "expected": expected,
                "verdict": "SKIP" if skip else ("PASS" if ok else "FAIL"),
                "detail": detail,
            }
        )

    def seed_system(self, name: str) -> str:
        status, payload, text = self.request(
            "POST",
            "/systems",
            key=OPERATOR_KEY,
            body={
                "name": name,
                "kind": "agent",
                "environment": "development",
                "owner": "acceptance",
                "adapter": "http",
            },
        )
        assert status < 300, f"system seed failed: {status} {text}"
        return payload["id"]

    def seed_finding(
        self, system_id: str, *, commit: str = "abc123", evidence_commit: str = "def456"
    ) -> str:
        """Create a real finding through the ordinary ingestion and critic path.

        The claim names ``commit`` and the linked verification evidence names
        ``evidence_commit``, so the critic has a mismatch to find unless the two are equal.
        """

        status, claim, text = self.request(
            "POST",
            "/claims",
            key=OPERATOR_KEY,
            body={
                "statement": f"All required tests pass for commit {commit}.",
                "claimant_system_id": system_id,
                "claim_type": "completion",
                "scope": {"system_id": system_id, "artifact": "demo", "commit": commit},
                "producer_event_id": f"accept-claim-{commit}",
            },
        )
        assert status < 300, f"claim seed failed: {status} {text}"
        claim_id = claim["id"]

        status, event, text = self.request(
            "POST",
            "/events",
            key=OPERATOR_KEY,
            body={
                "source_system_id": system_id,
                "event_type": "agent.completion_claim",
                "occurred_at": "2026-10-24T10:00:00Z",
                "correlation_id": f"accept-event-{commit}",
                "payload": {"claim_id": claim_id},
                "producer_event_id": f"accept-event-{commit}",
                "trust_class": "observed",
                "labels": ["acceptance"],
                "retention_class": "standard",
            },
        )
        assert status < 300, f"event seed failed: {status} {text}"
        event_id = event["id"]

        status, _payload, text = self.request(
            "POST",
            "/evidence",
            key=OPERATOR_KEY,
            body={
                "kind": "ci_test_result",
                "title": "CI run for another commit",
                "description": f"CI run for commit {evidence_commit}: all required checks passed.",
                "source_system_id": system_id,
                "source_ref": f"https://ci.example.invalid/run/{commit}",
                "scope": {
                    "system_id": system_id,
                    "artifact": "demo",
                    "commit": evidence_commit,
                },
                "captured_at": "2026-10-24T09:00:00Z",
                "valid_from": "2026-10-24T09:00:00Z",
                "integrity_method": "none",
                "relevance_to_claim": claim_id,
                "limitations": ["Synthetic acceptance evidence."],
                "sensitivity": "internal",
                "access_policy_ref": "acceptance",
                "retention_class": "standard",
                "producer_event_id": f"accept-evidence-{commit}",
            },
        )
        assert status < 300, f"evidence seed failed: {status} {text}"

        status, payload, text = self.request(
            "POST",
            "/critics/run",
            key=OPERATOR_KEY,
            body={"event_id": event_id, "critic_ids": ["c001-completion-vs-verification"]},
        )
        assert status == 200, f"critic run failed: {status} {text}"
        results = payload.get("results") or []
        assert results and results[0].get("finding_id"), f"no finding composed: {text}"
        return results[0]["finding_id"]

    def seed_counterevidence(self, finding_id: str, *, marker_commit: str = "9ab8") -> str:
        """Register an evidence item linked to a finding, for a disposition to weigh."""

        status, payload, text = self.request(
            "POST",
            "/evidence",
            key=OPERATOR_KEY,
            body={
                "kind": "ci_test_result",
                "title": "Independent counterevidence",
                "description": (
                    f"An independent rerun for commit {marker_commit} recorded a failed outcome."
                ),
                "source_system_id": "sys_independent",
                "source_ref": f"https://ci.example.invalid/run/{marker_commit}",
                "scope": {
                    "system_id": "sys_independent",
                    "artifact": "demo",
                    "commit": marker_commit,
                },
                "captured_at": "2026-10-24T11:00:00Z",
                "valid_from": "2026-10-24T11:00:00Z",
                "integrity_method": "none",
                "relevance_to_finding": finding_id,
                "limitations": ["Synthetic acceptance evidence."],
                "sensitivity": "internal",
                "access_policy_ref": "acceptance",
                "retention_class": "standard",
                "producer_event_id": f"accept-counter-{finding_id}",
            },
        )
        assert status < 300, f"counterevidence seed failed: {status} {text}"
        return payload["id"]

    def seed_restricted_finding(self, system_id: str) -> str:
        """A finding whose linked evidence carries a restricted marker."""

        status, claim, text = self.request(
            "POST",
            "/claims",
            key=OPERATOR_KEY,
            body={
                "statement": "All required tests pass for commit abc123.",
                "claimant_system_id": system_id,
                "claim_type": "completion",
                "scope": {"system_id": system_id, "artifact": "demo", "commit": "abc123"},
                "producer_event_id": "accept-restricted-claim",
            },
        )
        assert status < 300, f"claim seed failed: {status} {text}"
        claim_id = claim["id"]

        status, event, text = self.request(
            "POST",
            "/events",
            key=OPERATOR_KEY,
            body={
                "source_system_id": system_id,
                "event_type": "agent.completion_claim",
                "occurred_at": "2026-10-24T10:00:00Z",
                "correlation_id": "accept-restricted-event",
                "payload": {"claim_id": claim_id},
                "producer_event_id": "accept-restricted-event",
                "trust_class": "observed",
                "labels": ["acceptance"],
                "retention_class": "standard",
            },
        )
        assert status < 300, f"event seed failed: {status} {text}"
        event_id = event["id"]

        status, _payload, text = self.request(
            "POST",
            "/evidence",
            key=OPERATOR_KEY,
            body={
                "kind": "deployment_record",
                "title": "Restricted deploy log",
                "description": f"Deployment succeeded with token {MARKER} embedded.",
                "source_system_id": system_id,
                "source_ref": "https://deploy.example.invalid/run/1",
                "scope": {"system_id": system_id, "artifact": "demo", "commit": "abc123"},
                "captured_at": "2026-10-24T09:00:00Z",
                "valid_from": "2026-10-24T09:00:00Z",
                "integrity_method": "none",
                "relevance_to_claim": claim_id,
                "limitations": ["Synthetic acceptance evidence."],
                "sensitivity": "restricted",
                "access_policy_ref": "policy:restricted-operator-only",
                "retention_class": "standard",
                "producer_event_id": "accept-restricted-evidence",
            },
        )
        assert status < 300, f"evidence seed failed: {status} {text}"

        status, payload, text = self.request(
            "POST",
            "/critics/run",
            key=OPERATOR_KEY,
            body={"event_id": event_id, "critic_ids": ["c001-completion-vs-verification"]},
        )
        assert status == 200, f"critic run failed: {status} {text}"
        results = payload.get("results") or []
        assert results and results[0].get("finding_id"), f"no finding composed: {text}"
        return results[0]["finding_id"]

    def advance_to_verification(self, finding_id: str) -> str:
        """Walk a finding to `verification_pending` and return the completed job id."""

        status, payload, text = self.request(
            "POST",
            f"/findings/{finding_id}/rebuttals",
            key=OPERATOR_KEY,
            body={
                "submitted_by": "operator",
                "argument": "The linked evidence does not cover the claimed commit.",
                "requested_outcome": "no_change",
            },
        )
        assert status < 300, f"rebuttal failed: {status} {text}"

        status, payload, text = self.request(
            "POST",
            f"/findings/{finding_id}/verification-jobs",
            key=OPERATOR_KEY,
            body={"check_id": "critics.rerun-linked", "requested_by": "operator"},
        )
        assert status < 300, f"verification request failed: {status} {text}"
        job_id = payload.get("job", {}).get("id")
        assert job_id, f"no job id: {text}"

        status, payload, text = self.request(
            "POST",
            f"/findings/{finding_id}/verification-jobs/run",
            key=OPERATOR_KEY,
        )
        assert status < 300, f"verification run failed: {status} {text}"
        return job_id


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    if not _port_is_free(args.port):
        print(f"port {args.port} is already in use; choose another with --port", file=sys.stderr)
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="nyx-v002-acceptance-"))
    harness = Harness(args.port, workspace)
    try:
        harness.migrate()
        harness.start()
        _run(harness)
    finally:
        harness.stop()
        harness.client.close()

    receipt = {
        "port": args.port,
        "results": harness.results,
        "passed": sum(1 for r in harness.results if r["verdict"] == "PASS"),
        "failed": sum(1 for r in harness.results if r["verdict"] == "FAIL"),
        "skipped": sum(1 for r in harness.results if r["verdict"] == "SKIP"),
    }
    text = json.dumps(receipt, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    shutil.rmtree(workspace, ignore_errors=True)
    return 0 if receipt["failed"] == 0 else 1


def _cli(h: Harness, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a NYX CLI command against the harness's own store."""

    return subprocess.run(
        ["uv", "run", "nyx", *args],
        cwd=str(REPO),
        env=h.environment(),
        capture_output=True,
        text=True,
        check=False,
    )


def _audit_mutation_scenario(h: Harness) -> None:
    """GAP-05: a body mutated after its checkpoint must fail verification.

    The mutation is a body-only edit that leaves every stored hash label untouched, which is
    exactly the case a comparison that trusts the labels would pass.
    """

    env = h.environment()
    checkpoint = h.checkpoint
    checkpoint.parent.mkdir(parents=True, exist_ok=True)

    written = subprocess.run(
        ["uv", "run", "nyx", "audit", "checkpoint", "--json", "--file", str(checkpoint)],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert written.returncode == 0, f"checkpoint failed: {written.stdout}\n{written.stderr}"

    import sqlite3

    connection = sqlite3.connect(h.database)
    try:
        row = connection.execute(
            "SELECT sequence, reason FROM audit_entries ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "no audit entry to mutate"
        sequence, _reason = row
        # A body-only edit: the stored fields change and the stored hash labels do not, which is
        # exactly the case a comparison that trusts the labels would pass.
        connection.execute(
            "UPDATE audit_entries SET reason=? WHERE sequence=?",
            ("tampered body", sequence),
        )
        connection.commit()
    finally:
        connection.close()

    verified = subprocess.run(
        [
            "uv",
            "run",
            "nyx",
            "audit",
            "verify",
            "--against",
            str(checkpoint),
            "--json",
        ],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    h.record(
        "v002-gap-15",
        "refused",
        verified.returncode != 0,
        f"mutated body detected (exit {verified.returncode})",
    )


def _focused_test_scenario(h: Harness, case_id: str, expected: str, test_path: str) -> None:
    """Run a focused suite that covers a scenario the HTTP surface cannot express."""

    result = subprocess.run(
        [
            str(VENV_PYTHON),
            "-m",
            "pytest",
            test_path,
            "-o",
            "addopts=--import-mode=importlib",
            "-q",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    h.record(
        case_id,
        expected,
        result.returncode == 0,
        f"{test_path} exit {result.returncode}",
    )


def _run(h: Harness) -> None:
    operator_system = h.seed_system("acceptance-operator")

    # --- GAP-01: authentication and authorisation ---------------------------
    status, _payload, _text = h.request(
        "POST",
        "/systems",
        body={
            "name": "gap-sys",
            "kind": "agent",
            "environment": "development",
            "owner": "eval",
            "adapter": "http",
        },
    )
    h.record("v002-gap-01", "refused", status == 401, f"no key -> {status}")

    status, _payload, _text = h.request(
        "POST",
        "/systems",
        key="not-a-valid-key",
        body={
            "name": "gap-sys",
            "kind": "agent",
            "environment": "development",
            "owner": "eval",
            "adapter": "http",
        },
    )
    h.record("v002-gap-02", "refused", status == 401, f"invalid key -> {status}")

    status, _payload, _text = h.request(
        "POST",
        "/systems",
        key=READER_KEY,
        body={
            "name": "gap-sys",
            "kind": "agent",
            "environment": "development",
            "owner": "eval",
            "adapter": "http",
        },
    )
    h.record("v002-gap-03", "refused", status == 403, f"reader -> {status}")

    status, _payload, _text = h.request(
        "POST",
        "/systems",
        key=OPERATOR_KEY,
        body={
            "name": "gap-sys-ok",
            "kind": "agent",
            "environment": "development",
            "owner": "eval",
            "adapter": "http",
        },
    )
    h.record("v002-gap-04", "allowed", status < 300, f"operator -> {status}")

    status, _payload, _text = h.request("GET", "/health")
    h.record("v002-gap-05", "allowed", status == 200, f"health -> {status}")

    status, _payload, _text = h.request(
        "POST", "/critics/run", key=READER_KEY, body={"event_id": "evt_none"}
    )
    h.record("v002-gap-06", "refused", status == 403, f"reader run -> {status}")

    # --- GAP-02: a restricted marker must not be disclosed ------------------
    finding = h.seed_restricted_finding(operator_system)
    _status, _payload, detail_text = h.request("GET", f"/findings/{finding}", key=READER_KEY)
    h.record(
        "v002-gap-07",
        "masked",
        MARKER not in detail_text,
        "marker absent from finding detail" if MARKER not in detail_text else "MARKER DISCLOSED",
    )
    _status, _payload, list_text = h.request("GET", "/findings", key=READER_KEY)
    h.record(
        "v002-gap-08",
        "masked",
        MARKER not in list_text,
        "marker absent from findings list" if MARKER not in list_text else "MARKER DISCLOSED",
    )

    # --- GAP-03: citation validation ----------------------------------------
    target = h.seed_finding(operator_system)
    h.advance_to_verification(target)

    status, _payload, _text = h.request(
        "POST",
        f"/findings/{target}/dispositions",
        key=OPERATOR_KEY,
        body={
            "outcome": "inconclusive",
            "decided_by": "operator",
            "rationale": "The references need inspection.",
            "policy_version": "1.0",
            "counterevidence_ids": ["ev_does_not_exist"],
        },
    )
    h.record("v002-gap-09", "refused", 400 <= status < 500, f"unknown counterevidence -> {status}")

    other = h.seed_finding(operator_system, commit="def456", evidence_commit="abc123")
    other_job = h.advance_to_verification(other)
    status, _payload, _text = h.request(
        "POST",
        f"/findings/{target}/dispositions",
        key=OPERATOR_KEY,
        body={
            "outcome": "inconclusive",
            "decided_by": "operator",
            "rationale": "The references need inspection.",
            "policy_version": "1.0",
            "verification_run_ids": [other_job],
        },
    )
    h.record(
        "v002-gap-10",
        "refused",
        400 <= status < 500,
        f"another finding's job -> {status}",
    )

    pending = h.seed_finding(operator_system, commit="9ab8")
    status, payload, text = h.request(
        "POST",
        f"/findings/{pending}/rebuttals",
        key=OPERATOR_KEY,
        body={
            "submitted_by": "operator",
            "argument": "Needs a second look.",
            "requested_outcome": "no_change",
        },
    )
    assert status < 300, text
    status, payload, text = h.request(
        "POST",
        f"/findings/{pending}/verification-jobs",
        key=OPERATOR_KEY,
        body={"check_id": "critics.rerun-linked", "requested_by": "operator"},
    )
    assert status < 300, text
    pending_job = payload["job"]["id"]
    status, _payload, _text = h.request(
        "POST",
        f"/findings/{pending}/dispositions",
        key=OPERATOR_KEY,
        body={
            "outcome": "inconclusive",
            "decided_by": "operator",
            "rationale": "The references need inspection.",
            "policy_version": "1.0",
            "verification_run_ids": [pending_job],
        },
    )
    h.record(
        "v002-gap-11",
        "refused",
        400 <= status < 500,
        f"unfinished verification -> {status}",
    )

    # A valid disposition: real counterevidence plus a completed, case-bound job.
    status, payload, text = h.request(
        "POST",
        f"/findings/{target}/rebuttals",
        key=OPERATOR_KEY,
        body={
            "submitted_by": "operator",
            "argument": "The evidence covers another commit.",
            "requested_outcome": "no_change",
        },
    )
    assert status < 300, text
    status, payload, text = h.request(
        "POST",
        f"/findings/{target}/verification-jobs",
        key=OPERATOR_KEY,
        body={"check_id": "critics.rerun-linked", "requested_by": "operator"},
    )
    assert status < 300, text
    target_job = payload["job"]["id"]
    status, _payload, text = h.request(
        "POST",
        f"/findings/{target}/verification-jobs/run",
        key=OPERATOR_KEY,
    )
    assert status < 300, text
    status, payload, text = h.request(
        "GET",
        f"/verification-jobs/{target_job}",
        key=OPERATOR_KEY,
    )
    assert status == 200, text
    completed = payload.get("job", {}).get("status") == "succeeded"
    job_status = payload.get("job", {}).get("status")
    counterevidence_id = h.seed_counterevidence(target)
    status, _payload, text = h.request(
        "POST",
        f"/findings/{target}/dispositions",
        key=OPERATOR_KEY,
        body={
            "outcome": "inconclusive",
            "decided_by": "operator",
            "rationale": "The claim cannot be verified from the linked records.",
            "policy_version": "1.0",
            "verification_run_ids": [target_job],
            "counterevidence_ids": [counterevidence_id],
        },
    )
    h.record(
        "v002-gap-12",
        "allowed",
        status < 300 and completed,
        f"valid disposition -> {status} (job status={job_status}) body={text[:200]}",
    )

    # --- harness-level scenarios --------------------------------------------
    _audit_mutation_scenario(h)

    h.stop()
    h.start()

    # --- GAP-07: work accepted before a restart is assessed afterwards -----
    pending_system = h.seed_system("acceptance-pending")
    status, claim, text = h.request(
        "POST",
        "/claims",
        key=OPERATOR_KEY,
        body={
            "statement": "All required tests pass for commit abc123.",
            "claimant_system_id": pending_system,
            "claim_type": "completion",
            "scope": {"system_id": pending_system, "artifact": "demo", "commit": "abc123"},
            "producer_event_id": "accept-pending-claim",
        },
    )
    assert status < 300, text
    status, event, text = h.request(
        "POST",
        "/events",
        key=OPERATOR_KEY,
        body={
            "source_system_id": pending_system,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-24T10:00:00Z",
            "correlation_id": "accept-pending-event",
            "payload": {"claim_id": claim["id"]},
            "producer_event_id": "accept-pending-event",
            "trust_class": "observed",
            "labels": ["acceptance"],
            "retention_class": "standard",
        },
    )
    assert status < 300, text
    status, _payload, text = h.request(
        "POST",
        "/evidence",
        key=OPERATOR_KEY,
        body={
            "kind": "ci_test_result",
            "title": "CI run for another commit",
            "description": "CI run for commit def456: all required checks passed.",
            "source_system_id": pending_system,
            "source_ref": "https://ci.example.invalid/run/pending",
            "scope": {"system_id": pending_system, "artifact": "demo", "commit": "def456"},
            "captured_at": "2026-10-24T09:00:00Z",
            "valid_from": "2026-10-24T09:00:00Z",
            "integrity_method": "none",
            "relevance_to_claim": claim["id"],
            "limitations": ["Synthetic acceptance evidence."],
            "sensitivity": "internal",
            "access_policy_ref": "acceptance",
            "retention_class": "standard",
            "producer_event_id": "accept-pending-evidence",
        },
    )
    assert status < 300, text

    def backlog() -> dict[str, int]:
        result = _cli(h, "assessment", "backlog", "--json")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    before_restart = backlog()
    h.stop()
    during_restart = backlog()
    h.start()
    drained = _cli(h, "assessment", "drain", "--json")
    assert drained.returncode == 0, drained.stderr
    after_drain = backlog()
    h.record(
        "v002-gap-16",
        "allowed",
        before_restart["queued"] >= 1
        and during_restart["queued"] >= 1
        and json.loads(drained.stdout)["count"] >= 1
        and after_drain["queued"] == 0,
        f"queued before={before_restart['queued']} during={during_restart['queued']} "
        f"drained={json.loads(drained.stdout)['count']} after={after_drain['queued']}",
    )

    _focused_test_scenario(
        h,
        "v002-gap-13",
        "allowed",
        "tests/unit/test_v002_llm_target.py",
    )
    _focused_test_scenario(
        h,
        "v002-gap-17",
        "refused",
        "tests/unit/test_agent_transcript_adapter.py",
    )
    _focused_test_scenario(
        h,
        "v002-gap-18",
        "refused",
        "tests/security/test_probe_deadlines.py",
    )


if __name__ == "__main__":
    raise SystemExit(main())
