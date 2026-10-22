"""Watch an agent: file its claims in NYX and let the critics judge them.

An agent's transcript is a claim set. Every assistant message asserts something; every tool call
and its result is the evidence that may or may not back it. This adapter reads that transcript
read-only and files it in NYX, so NYX's critics can answer the question no agent can answer
itself: how much of what was said is actually backed by what was done?

Read-only on the source store: the watcher never writes to the agent's own database.

Usage:
    uv run python scripts/nyx_watch_agent.py --db ~/.hermes/state.db --limit 40
    uv run python scripts/nyx_watch_agent.py --db ~/.hermes/state.db --session <id>
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
PORT = 8641
BASE = f"http://127.0.0.1:{PORT}"


def _default_db() -> Path:
    """Locate the agent's own transcript store.

    Hermes keeps it under %LOCALAPPDATA%, not the home directory, so probe both.
    """

    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "state.db",
        Path.home() / ".hermes" / "state.db",
        Path.home() / "AppData" / "Local" / "hermes" / "state.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


# Tool calls whose result is not evidence about the world: navigation, not observation.
NON_EVIDENCE_TOOLS = {"write_file", "patch"}


def free_port(port: int) -> None:
    """Kill whatever is LISTENING on `port`.

    `shell=True` on Windows is cmd.exe, so the bash `$(...)`/`awk` form silently does nothing.
    Parse `netstat` in Python instead, and retry: a server that is shutting down may reappear.
    """

    for _ in range(5):
        pids: set[str] = set()
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
        except Exception:
            return
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    pids.add(parts[-1])
        if not pids:
            return
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, text=True)
        time.sleep(0.6)


def clean(text: str, limit: int = 1800) -> str:
    """Collapse a transcript message into a citable statement."""

    if not text:
        return ""
    text = unicodedata.normalize("NFC", text).strip()
    # Fence blocks are transcripts of files, not claims. Drop them but keep the prose.
    parts: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            continue
        parts.append(line)
    joined = " ".join(" ".join(parts).split())
    return joined[:limit]


def read_transcript(db_path: Path, session_id: str | None, limit: int) -> list[dict]:
    """Return assistant turns with their tool calls and results, newest first."""

    uri = f"file:{db_path.as_posix()}?mode=ro"
    # Read-only URI so a watcher can never write to the agent's own transcript store.
    conn = sqlite3.connect(uri, uri=True)  # noqa: S310 - sqlite file: URI, not a network URL
    conn.row_factory = sqlite3.Row
    try:
        if session_id is None:
            row = conn.execute(
                "SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return []
            session_id = row["id"]

        turns: list[dict] = []
        cursor = conn.execute(
            "SELECT id, content, tool_calls FROM messages "
            "WHERE session_id = ? AND role = 'assistant' AND content IS NOT NULL "
            "AND TRIM(content) <> '' ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        assistant_ids = {r["id"] for r in cursor.fetchall()}
        if not assistant_ids:
            return []

        results: dict[str, str] = {}
        for r in conn.execute(
            "SELECT tool_call_id, content FROM messages WHERE session_id = ? AND role = 'tool'",
            (session_id,),
        ):
            if r["tool_call_id"]:
                results[r["tool_call_id"]] = r["content"] or ""

        for r in conn.execute(
            "SELECT id, content, tool_calls FROM messages "
            "WHERE session_id = ? AND role = 'assistant' AND content IS NOT NULL "
            "AND TRIM(content) <> '' ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ):
            calls: list[dict] = []
            raw = r["tool_calls"]
            if raw:
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = []
                if isinstance(parsed, list):
                    for c in parsed:
                        fn = (c or {}).get("function") or {}
                        name = fn.get("name") or ""
                        args = fn.get("arguments") or ""
                        if name in NON_EVIDENCE_TOOLS:
                            continue
                        try:
                            arg_obj = json.loads(args) if isinstance(args, str) else args
                        except Exception:
                            arg_obj = {"raw": str(args)[:400]}
                        calls.append(
                            {
                                "call_id": (c or {}).get("call_id") or (c or {}).get("id") or "",
                                "tool": name,
                                "args": arg_obj,
                                "result": results.get(
                                    (c or {}).get("call_id") or (c or {}).get("id") or "",
                                    "",
                                ),
                            }
                        )
            turns.append({"id": r["id"], "statement": clean(r["content"]), "calls": calls})
        return turns
    finally:
        conn.close()


def call(method: str, path: str, body: dict | None = None, timeout: int = 30, quiet: bool = False):
    """One HTTP call against the local NYX server, matching the project's httpx convention."""

    url = BASE + path
    try:
        response = httpx.request(
            method,
            url,
            json=body,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        try:
            return response.status_code, response.json()
        except Exception:
            return response.status_code, {}
    except httpx.HTTPError as exc:
        if not quiet:
            print(f"      !! {method} {path} failed: {type(exc).__name__}")
        return 0, {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--session", default=None)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--keep", action="store_true", help="do not tear the server down")
    args = ap.parse_args()

    global BASE
    BASE = f"http://127.0.0.1:{args.port}"

    db_path = args.db or _default_db()

    if not db_path.exists():
        print(f"no transcript database at {db_path}")
        return 2

    turns = read_transcript(db_path, args.session, args.limit)
    if not turns:
        print("no assistant turns found")
        return 1
    print(f"transcript: {len(turns)} assistant turns")

    tmp = Path(tempfile.mkdtemp(prefix="nyx-watch-"))
    env = dict(__import__("os").environ)
    env.update(
        NYX_DATABASE_URL=f"sqlite:///{(tmp / 'watch.sqlite3').as_posix()}",
        NYX_DATA_DIR=str(tmp / "data"),
        NYX_HOST="127.0.0.1",
        NYX_PORT=str(args.port),
        NYX_AUTH_MODE="none",
        NYX_LLM_ENABLED="false",
        NYX_RATE_LIMIT_ENABLED="false",
    )
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    free_port(args.port)
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    # Held open for the server's lifetime and closed in the finally block below.
    log = open(tmp / "srv.log", "w", encoding="utf-8")  # noqa: SIM115 - lifetime is the run
    srv = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "nyx.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
        ],
        cwd=REPO,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        ready = False
        for _ in range(60):
            try:
                st, _ = call("GET", "/api/v1/ready", quiet=True)
                if st == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ready:
            print("server never became ready")
            return 2

        # Prove we are talking to OUR server, not a stale one from an earlier run.
        st, body = call("GET", "/api/v1/systems")
        if not (st == 200 and body.get("items") == []):
            print(
                f"ABORT: stale server on port {args.port} "
                f"(existing systems={len(body.get('items', []))})"
            )
            return 2

        # One event per turn: the agent observed something and asserted something about it.
        st, body = call(
            "POST",
            "/api/v1/systems",
            {
                "name": "hermes-agent",
                "kind": "agent",
                "environment": "production",
                "owner": "kallmedis",
                "capabilities": ["events.write", "findings.read"],
                "adapter": "custom",
                "metadata": {"transcript_db": str(args.db)},
            },
        )
        sysid = body.get("id")
        print(f"system: {sysid}")

        filed = 0
        unbacked = 0
        for turn in turns:
            statement = turn["statement"]
            if not statement:
                continue
            eid = f"evt_watch_{turn['id']}"
            st, _ = call(
                "POST",
                "/api/v1/events",
                {
                    "id": eid,
                    "schema_version": "1.0",
                    "source_system_id": sysid,
                    "event_type": "agent.claim.submitted",
                    "occurred_at": "2026-10-22T00:00:00Z",
                    "received_at": "2026-10-22T00:00:00Z",
                    "correlation_id": eid,
                    "actor": {"id": sysid, "kind": "agent", "display_name": "hermes"},
                    "subject": {
                        "id": "hermes-agent",
                        "kind": "agent",
                        "display_name": "hermes-agent",
                    },
                    "payload": {
                        "claim_id": f"clm_watch_{turn['id']}",
                        "turn_id": turn["id"],
                        "tool_count": len(turn["calls"]),
                    },
                    "source_ref": f"hermes://{args.session or 'latest'}/{turn['id']}",
                    "trust_class": "observed",
                    "labels": ["agent-watch"],
                    "retention_class": "standard",
                    "producer_event_id": eid,
                },
            )
            if st not in (200, 201):
                continue
            st, _ = call(
                "POST",
                "/api/v1/claims",
                {
                    "id": f"clm_watch_{turn['id']}",
                    "schema_version": "1.0",
                    "statement": statement,
                    "claimant_system_id": sysid,
                    "claim_type": "factual",
                    "scope": {
                        "system_id": sysid,
                        "artifact": "hermes-agent",
                        "environment": "production",
                        "conditions": [f"transcript turn {turn['id']}"],
                    },
                    "source_event_ids": [eid],
                    "status": "unassessed",
                    "producer_event_id": f"clm_watch_{turn['id']}",
                },
            )
            if st not in (200, 201):
                continue
            for c in turn["calls"]:
                result_text = c["result"]
                # An error result is a failed observation, not a passing one.
                failed = result_text.strip().startswith(("{",)) and '"error"' in result_text[:400]
                st, _ = call(
                    "POST",
                    "/api/v1/evidence",
                    {
                        "id": f"ev_watch_{c['call_id'][:12]}",
                        "schema_version": "1.0",
                        "kind": "measurement" if not failed else "ci_test_result",
                        "title": f"{c['tool']} observation",
                        "description": f"Tool {c['tool']} invoked by the agent.",
                        "source_system_id": sysid,
                        "source_ref": f"hermes://tool/{c['call_id']}",
                        "locator": f"tool:{c['tool']}",
                        "scope": {
                            "system_id": sysid,
                            "artifact": "hermes-agent",
                            "environment": "production",
                        },
                        "captured_at": "2026-10-22T00:00:00Z",
                        "valid_from": "2026-10-22T00:00:00Z",
                        "integrity_method": "hash",
                        "producer_identity": {
                            "id": sysid,
                            "kind": "agent",
                            "display_name": "hermes",
                        },
                        "verification": {
                            "result": "failed" if failed else "verified",
                            "method": "observed",
                            "verified_at": "2026-10-22T00:00:00Z",
                            "verified_by": "agent-watch",
                        },
                        "relevance_to_claim": f"clm_watch_{turn['id']}",
                        "relevance_note": "Tool call and result backing this turn's statement.",
                        "limitations": [
                            "Tool output is the agent's own observation, not an independent check."
                        ],
                        "sensitivity": "internal",
                        "access_policy_ref": "policy:evidence-internal-read",
                        "retention_class": "standard",
                        "producer_event_id": f"ev_watch_{c['call_id'][:12]}",
                    },
                )
            filed += 1
            if not turn["calls"]:
                unbacked += 1

        print(f"claims filed: {filed}   unbacked (no tool call): {unbacked}")

        if unbacked:
            print("\n=== claims with NO tool call behind them ===")
            for turn in turns:
                if turn["calls"] or not turn["statement"]:
                    continue
                print(f"  turn {turn['id']}: {turn['statement'][:160]}")

        print("\n=== running the critic pack ===")
        for turn in turns:
            call("POST", "/api/v1/critics/run", {"event_id": f"evt_watch_{turn['id']}"})

        st, body = call("GET", "/api/v1/findings?limit=50")
        items = body.get("items", [])
        print(f"findings: {len(items)}")
        by_cat: dict[str, int] = {}
        for f in items:
            by_cat[f["category"]] = by_cat.get(f["category"], 0) + 1
        for cat, n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
            print(f"  {cat:24s} {n}")

        print("\n=== sample findings ===")
        for f in items[:5]:
            print(f"\n  [{f['severity']}] {f['title']}")
            print(f"    {f['summary'][:200]}")
            print(f"    limitations: {f.get('limitations', [])[:1]}")

        st, body = call("GET", "/api/v1/claims?limit=1")
        print(f"\nclaims in store: {body.get('items') and len(body['items'])}")
        return 0
    finally:
        if not args.keep:
            srv.terminate()
            try:
                srv.wait(timeout=8)
            except Exception:
                srv.kill()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
