#!/usr/bin/env python3
"""Independent acceptance harness for NYX — talks to a real uvicorn server over
real HTTP and drives the real CLI as subprocesses. No TestClient, no nyx imports.
"""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
PORT = int(os.environ.get("NYX_HARNESS_PORT", "8097"))
BASE = f"http://127.0.0.1:{PORT}/api/v1"
SECRETS = {
    "password": "hunter2-super-secret",
    "bearer": "Bearer sk-live-abc123def456",
    "aws": "AKIAIOSFODNN7EXAMPLE",
    "pem": "-----BEGIN PRIVATE KEY-----\nMIIEvQ==\n-----END PRIVATE KEY-----",
}

results: list[tuple[str, str, str]] = []  # (num, verdict, detail)
_failures: list[str] = []


def record(num: str, ok: bool, detail: str = "", skip: bool = False) -> None:
    verdict = "SKIP" if skip else ("PASS" if ok else "FAIL")
    results.append((num, verdict, detail))
    if not skip and not ok:
        _failures.append(num)
    print(f"  [{verdict}] {num}  {detail}")


def run_cli(
    *args: str, env_extra: dict | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("NYX_DATABASE_URL", os.environ.get("NYX_DATABASE_URL", ""))
    if env_extra:
        env.update(env_extra)
    cmd = ["uv", "run", "nyx", *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, env=env, cwd=str(cwd or REPO)
    )


def port_is_free(port: int) -> bool:
    """Return True when nothing is already listening on ``port``.

    This guard exists because the harness once produced a FALSE PASS by talking to an
    orphaned server from a previous run: readiness polling cannot tell our own server from
    somebody else's already answering on the same port. Never start a check run against a
    server we did not launch.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_for_server(proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            httpx.get(f"{BASE}/health", timeout=1.0)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def count_events(db_path: Path) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        con.close()


def scan_db_file_for_secrets(db_path: Path) -> list[str]:
    raw = db_path.read_bytes()
    return [v for v in SECRETS.values() if v.encode() in raw]


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="nyx-acc-"))
    db_path = tmpdir / "acc.db"
    db_url = f"sqlite:///{db_path.as_posix()}"

    # --- refuse to run against a server we did not start --------------------
    if not port_is_free(PORT):
        print(
            f"PORT {PORT} IS ALREADY IN USE.\n"
            "A previous run probably left an orphaned uvicorn behind, and talking to it "
            "would produce meaningless or falsely-passing results. Kill it and retry:\n"
            f"  MSYS_NO_PATHCONV=1 taskkill /F /IM uvicorn.exe\n"
            "Or choose another port with NYX_HARNESS_PORT."
        )
        return 2

    # --- start server -------------------------------------------------------
    env = os.environ.copy()
    env["NYX_DATABASE_URL"] = db_url
    env["PYTHONPATH"] = str(REPO)

    # run migrations
    mig = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
        timeout=60,
    )
    if mig.returncode != 0:
        print(f"MIGRATION FAILED:\n{mig.stderr[-800:]}")
        return 2

    server_log = tmpdir / "server.log"
    server = subprocess.Popen(
        [
            # Launch uvicorn with the venv interpreter directly, NOT through `uv run`.
            # `uv run` spawns a wrapper process, so killing the wrapper left the real
            # uvicorn child alive holding the port -- which is exactly how the orphaned
            # servers that produced a false pass got created.
            str(VENV_PYTHON),
            "-m",
            "uvicorn",
            "--factory",
            "nyx.api.app:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        env=env,
        cwd=str(REPO),
        # NEVER use subprocess.PIPE here without draining it. The server writes a JSON
        # access-log line per request to stderr; once the OS pipe buffer fills, the server
        # blocks on write and every later request times out. That deadlock is invisible
        # (the server is alive, just wedged) and looks exactly like a slow endpoint.
        stdout=server_log.open("w"),
        stderr=subprocess.STDOUT,
    )

    try:
        if not wait_for_server(server):
            print("SERVER FAILED TO START")
            if server.poll() is None:
                server.kill()
            return 2
        os.environ["NYX_DATABASE_URL"] = db_url
        _run_checks(db_path, db_url, tmpdir)
    finally:
        server.kill()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)

    # --- summary ------------------------------------------------------------
    print()
    print("=" * 60)
    for num, verdict, detail in results:
        print(f"  {num:>3}  {verdict}  {detail}")
    print("=" * 60)
    if _failures:
        print(f"FAILED ({len(_failures)}): {_failures}")
        return 1
    print("ALL PASS / SKIP")
    return 0


def _critic_slice(client: httpx.Client, sys_id: str | None) -> None:
    """Scenarios 4 and 5: the completion-claim vertical slice (SPEC §21).

    Scenario 5: a completion claim naming commit ``abc123``, whose linked verification
    evidence covers a DIFFERENT commit (``def456``), must produce a scoped finding.

    Scenario 4: the same shape with SAME-commit evidence must produce NO finding. This is
    the false-positive guard, and it is the more important of the two: a critic that fires on
    everything is worthless.
    """
    if not sys_id:
        record("4", False, "no sys_id")
        record("5", False, "no sys_id")
        return

    artifact = "ghost-factory-demo"
    critic = "c001-completion-vs-verification"

    def claim(commit: str, tag: str) -> str | None:
        r = client.post(
            f"{BASE}/claims",
            json={
                "statement": f"All required tests pass for commit {commit}.",
                "claimant_system_id": sys_id,
                "claim_type": "completion",
                "scope": {"system_id": sys_id, "artifact": artifact, "commit": commit},
                "producer_event_id": f"acc-claim-{tag}",
            },
        )
        if r.status_code not in (200, 201):
            print(f"    DEBUG claim {tag}: {r.status_code} {r.text[:250]}")
            return None
        return r.json().get("id")

    def event(claim_id: str, tag: str) -> str | None:
        r = client.post(
            f"{BASE}/events",
            json={
                "source_system_id": sys_id,
                "event_type": "agent.completion_claim",
                "occurred_at": "2026-10-19T10:00:00Z",
                "correlation_id": f"acc-critic-{tag}",
                "payload": {"claim_id": claim_id},
                "producer_event_id": f"acc-critic-evt-{tag}",
                "trust_class": "observed",
                "labels": ["critic-slice"],
                "retention_class": "standard",
            },
        )
        if r.status_code not in (200, 201):
            print(f"    DEBUG event {tag}: {r.status_code} {r.text[:250]}")
            return None
        return r.json().get("id")

    def evidence(claim_id: str, commit: str, tag: str) -> None:
        r = client.post(
            f"{BASE}/evidence",
            json={
                "kind": "ci_test_result",
                "title": "Required integration suite passed",
                "description": f"CI run for commit {commit}: all required checks passed.",
                "source_ref": f"https://ci.example.invalid/run/{tag}",
                # EvidenceInput requires a source_system_id or source_name; without one the
                # POST is rejected, the evidence never links, and C-001 then fires for the
                # WRONG reason (a verification gap instead of a commit mismatch).
                "source_system_id": sys_id,
                "scope": {"system_id": sys_id, "artifact": artifact, "commit": commit},
                "captured_at": "2026-10-19T09:00:00Z",
                "valid_from": "2026-10-19T09:00:00Z",
                "integrity_method": "none",
                "relevance_to_claim": claim_id,
                "limitations": ["Synthetic acceptance-harness evidence."],
                "sensitivity": "internal",
                "access_policy_ref": "harness",
                "retention_class": "standard",
                "producer_event_id": f"acc-ev-{tag}",
            },
        )
        if r.status_code not in (200, 201):
            print(f"    DEBUG evidence {tag}: {r.status_code} {r.text[:250]}")

    def run_critic(event_id: str) -> dict | None:
        r = client.post(f"{BASE}/critics/run", json={"event_id": event_id, "critic_ids": [critic]})
        if r.status_code != 200:
            print(f"    DEBUG critics/run: {r.status_code} {r.text[:250]}")
            return None
        results = r.json().get("results") or []
        return results[0] if results else None

    # --- scenario 5: evidence covers a DIFFERENT commit -> must fire ---
    claim5 = claim("abc123", "5")
    if claim5 is None:
        record("5", False, "claim not created")
    else:
        ev5 = event(claim5, "5")
        evidence(claim5, "def456", "5")
        out5 = run_critic(ev5) if ev5 else None
        fired = bool(out5 and out5.get("finding_id"))
        record(
            "5",
            fired,
            f"result={out5.get('result') if out5 else None} "
            f"finding={out5.get('finding_id') if out5 else None}",
        )

    # --- scenario 4: same-commit passing evidence -> must NOT fire ---
    claim4 = claim("abc123", "4")
    if claim4 is None:
        record("4", False, "claim not created")
    else:
        ev4 = event(claim4, "4")
        evidence(claim4, "abc123", "4")
        out4 = run_critic(ev4) if ev4 else None
        quiet = bool(out4 and out4.get("result") == "no_finding" and not out4.get("finding_id"))
        record(
            "4",
            quiet,
            f"result={out4.get('result') if out4 else None} "
            f"finding={out4.get('finding_id') if out4 else None}",
        )


def _run_checks(db_path: Path, db_url: str, tmpdir: Path) -> None:
    client = httpx.Client(timeout=20.0)

    # ---- Scenario 1: ingest, retrieve, trace ----
    r = client.post(
        f"{BASE}/systems",
        json={
            "name": "acc-sys",
            "kind": "agent",
            "environment": "development",
            "owner": "harness",
            "capabilities": ["events.write"],
            "adapter": "http",
        },
    )
    if r.status_code not in (200, 201):
        print(f"    DEBUG sys reg: {r.status_code} {r.text[:300]}")
    sys_id = r.json().get("id") if r.status_code in (200, 201) else None
    ev = {
        "source_system_id": sys_id,
        "event_type": "agent.completion_claim",
        "occurred_at": "2026-10-19T10:00:00Z",
        "correlation_id": "acc-corr-1",
        "payload": {"claim": "tests pass", "commit": "abc123", **SECRETS},
        "producer_event_id": "acc-evt-1",
        "trust_class": "observed",
        "labels": ["acc"],
        "retention_class": "standard",
    }
    r = client.post(f"{BASE}/events", json=ev)
    if r.status_code not in (200, 201):
        print(f"    DEBUG ev post: {r.status_code} {r.text[:300]}")
    ev_id = r.json().get("id") if r.status_code in (200, 201) else None
    r2 = client.get(f"{BASE}/events/{ev_id}")
    traced = r2.status_code == 200 and r2.json().get("source_system_id") == sys_id
    record("1", traced and bool(ev_id), f"sys={sys_id} ev={ev_id}")

    # ---- Scenario 2: idempotency + different producer ----
    r3 = client.post(f"{BASE}/events", json=ev)
    if r3.status_code == 409:
        print(f"    DEBUG 409 body: {r3.text[:300]}")
    dup_id = r3.json().get("id") if r3.status_code < 500 else None
    idempotent = r3.status_code == 409 and dup_id is None
    ev2 = {**ev, "producer_event_id": "acc-evt-2"}
    r4 = client.post(f"{BASE}/events", json=ev2)
    new_id = r4.json().get("id") if r4.status_code in (200, 201) else None
    record(
        "2",
        idempotent and new_id != ev_id and bool(new_id),
        f"dup_status={r3.status_code} new_id={new_id}",
    )

    # ---- Scenario 3: invalid schema ----
    rows_before = count_events(db_path)
    bad = {"source_system_id": sys_id, "event_type": "x"}
    r5 = client.post(f"{BASE}/events", json=bad)
    structured = False
    try:
        err = r5.json()
        structured = "code" in err and "message" in err
    except Exception:
        pass
    rows_after = count_events(db_path)
    record(
        "3",
        400 <= r5.status_code < 500 and structured and rows_after == rows_before,
        f"status={r5.status_code} rows_before={rows_before} rows_after={rows_after}",
    )

    # ---- Scenarios 4 & 5: deterministic critic vertical slice ----
    _critic_slice(client, sys_id)

    # ---- Scenario 14: secret redaction in DB file ----
    leaks_file = scan_db_file_for_secrets(db_path)
    r6 = client.get(f"{BASE}/events/{ev_id}")
    stored_text = r6.text
    leaks_api = [v for v in SECRETS.values() if v in stored_text]
    record(
        "14",
        not leaks_file and not leaks_api,
        f"file_leaks={len(leaks_file)} api_leaks={len(leaks_api)}",
    )

    # ---- Scenario 18: not-ready when migrations behind ----
    fresh_db = db_path.parent / "fresh.db"
    fresh_url = f"sqlite:///{fresh_db.as_posix()}"
    port2 = PORT + 1
    if not port_is_free(port2):
        record("18", False, f"port {port2} already in use; cannot test readiness honestly")
        return
    env2 = os.environ.copy()
    env2["NYX_DATABASE_URL"] = fresh_url
    env2["PYTHONPATH"] = str(REPO)
    server2 = subprocess.Popen(
        [
            # Same reason as the main server: never wrap in `uv run`, or the kill below
            # leaves an orphaned uvicorn holding the port.
            str(VENV_PYTHON),
            "-m",
            "uvicorn",
            "--factory",
            "nyx.api.app:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port2),
        ],
        env=env2,
        cwd=str(REPO),
        # Same pipe-deadlock hazard as the main server: never PIPE without draining.
        stdout=(tmpdir / "server2.log").open("w"),
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 20
        ready_status = None
        while time.time() < deadline:
            if server2.poll() is not None:
                break
            try:
                rr = httpx.get(f"http://127.0.0.1:{port2}/api/v1/ready", timeout=1.0)
                ready_status = rr.status_code
                break
            except Exception:
                time.sleep(0.2)
        not_ready = ready_status == 503
        ing = httpx.post(f"http://127.0.0.1:{port2}/api/v1/events", json=ev, timeout=5.0)
        safe_fail = ing.status_code >= 400  # any non-2xx = safe failure
        record("18", not_ready and safe_fail, f"ready={ready_status} ingest={ing.status_code}")
    finally:
        server2.kill()
        try:
            server2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # Windows can hold the SQLite file handle for a moment after the process exits.
        # Cleanup must never crash the run, so retry briefly and then give up quietly.
        for _ in range(10):
            try:
                fresh_db.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.3)

    # ---- SKIP scenarios ----
    skips = {
        "6-13": "rebuttals/verification/dispositions not implemented (Phase 3)",
        "15": "prompt-injection tool-access guard not implemented",
        "16": "verification-tool sandbox not implemented",
        "17": "audit-history mutation detection not implemented",
        "19": "LLM-output validation not implemented",
        "20-24": "doctor/auth/self-consistency not fully implemented",
    }
    for num, reason in skips.items():
        record(num, False, reason, skip=True)

    # ---- CLI checks ----
    r = run_cli("doctor")
    record("CLI:doctor", r.returncode == 0, f"rc={r.returncode}")

    # `nyx system register` takes a PATH to a manifest file, not an inline JSON string.
    # `adapter` is required by SystemManifestInput and has no default.
    manifest_file = tmpdir / "cli_manifest.json"
    manifest_file.write_text(
        json.dumps(
            {
                "name": "cli-acc",
                "kind": "agent",
                "environment": "development",
                "owner": "harness",
                "adapter": "cli",
            }
        ),
        encoding="utf-8",
    )
    r = run_cli("system", "register", str(manifest_file))
    if r.returncode != 0:
        print(
            f"    DEBUG cli reg: rc={r.returncode} stdout={r.stdout[:200]} stderr={r.stderr[:300]}"
        )
    cli_reg_ok = r.returncode == 0
    r = run_cli("system", "list")
    cli_list_ok = r.returncode == 0 and "cli-acc" in r.stdout
    if not cli_list_ok:
        print(
            f"    DEBUG cli list: rc={r.returncode} stdout={r.stdout[:200]} stderr={r.stderr[:200]}"
        )
    record(
        "CLI:system round-trip",
        cli_reg_ok and cli_list_ok,
        f"reg_rc={cli_reg_ok} list_rc={r.returncode}",
    )

    if sys_id:
        ev_file = tmpdir / "cli_ev.json"
        ev_file.write_text(
            json.dumps(
                {
                    "source_system_id": sys_id,
                    "event_type": "agent.completion_claim",
                    "occurred_at": "2026-10-19T10:00:00Z",
                    "correlation_id": "cli-corr-1",
                    "payload": {"claim": "ok", "commit": "def456"},
                    "producer_event_id": "cli-evt-1",
                    "trust_class": "observed",
                    "retention_class": "standard",
                }
            )
        )
        r = run_cli("ingest", "event", str(ev_file))
        record("CLI:ingest event", r.returncode == 0, f"rc={r.returncode}")
    else:
        record("CLI:ingest event", False, "skipped: no sys_id")

    bad_file = tmpdir / "bad_ev.json"
    bad_file.write_text("not valid json {{{")
    r = run_cli("ingest", "event", str(bad_file))
    record("CLI:invalid input rejected", r.returncode != 0, f"rc={r.returncode}")

    client.close()


if __name__ == "__main__":
    sys.exit(main())
