"""Independent probe: a full round-trip through the MCP server with REAL data in a live NYX.

Seeds a system + completion claim + evidence via the HTTP API, runs the deterministic critics to
raise a real finding, then reads the whole record back through the MCP stdio session and asserts
the finding id the chat layer would cite is the same one NYX raised.

Also drives one write tool: nyx_submit_rebuttal, and confirms it appends (the finding is not
overwritten) by reading the history back through MCP.
"""

import json
import subprocess
import sys

import httpx

BASE = "http://127.0.0.1:8000/api/v1"
failures: list[str] = []
_id = 0
PROC = None


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def send(method, params=None, notify=False):
    global _id
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if not notify:
        _id += 1
        msg["id"] = _id
    PROC.stdin.write(json.dumps(msg) + "\n")
    PROC.stdin.flush()
    if notify:
        return None
    while True:
        line = PROC.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout")
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue


def call(name, args):
    """Return the tool payload. The tool layer wraps every result, so this is always an object."""

    r = send("tools/call", {"name": name, "arguments": args})
    content = (r.get("result") or {}).get("content", [])
    return json.loads("".join(b.get("text", "") for b in content))


def post(path, payload):
    r = httpx.post(f"{BASE}{path}", json=payload, timeout=30)
    return r


def main():
    global PROC
    with httpx.Client(timeout=30) as c:
        # --- seed via the HTTP API (the same interface any other system uses) ---
        r = c.post(
            f"{BASE}/systems",
            json={
                "name": "mcp-roundtrip",
                "kind": "repository",
                "environment": "staging",
                "owner": "probe",
                "capabilities": ["events.write", "findings.read", "rebuttals.write"],
                "adapter": "http",
            },
        )
        check("seed: register system", r.status_code in (200, 201), str(r.status_code))
        sys_id = r.json()["id"]
        print(f"      system: {sys_id}")

        # A completion claim is a CLAIM plus an event that carries its id. C-001 reads the claim
        # from the store, not from the event payload -- posting a payload-only event is not enough.
        # Idempotency is identity-based: the same producer_event_id returns the SAME record id
        # (409), so this probe can be re-run without seeding twice.
        claim_r = c.post(
            f"{BASE}/claims",
            json={
                "statement": "All required tests pass for commit zzz999.",
                "claimant_system_id": sys_id,
                "claim_type": "completion",
                "scope": {"system_id": sys_id, "artifact": "mcp-roundtrip", "commit": "zzz999"},
                "producer_event_id": "mcp-roundtrip-claim-1",
            },
        )
        check(
            "seed: register completion claim (idempotent)",
            claim_r.status_code in (201, 409),
            f"{claim_r.status_code} {claim_r.text[:160]}",
        )
        body = claim_r.json()
        claim_id = body.get("id") or body.get("record_id")
        if claim_id is None:
            # a DUPLICATE_EVENT names the existing record in prose, not as a field
            message = body.get("message", "")
            if "record " in message:
                claim_id = message.split("record ", 1)[1].split(";", 1)[0].strip()
        check("seed: the claim exists", bool(claim_id), str(claim_id))
        print(f"      claim: {claim_id}")

        # Idempotency: the same producer_event_id is the identity. Replaying must not duplicate,
        # so this probe can be re-run without seeding twice.
        r = c.post(
            f"{BASE}/events",
            json={
                "source_system_id": sys_id,
                "event_type": "agent.completion_claim",
                "occurred_at": "2026-10-20T10:00:00Z",
                "correlation_id": "mcp-roundtrip-1",
                "payload": {"claim_id": claim_id},
                "producer_event_id": "mcp-roundtrip-evt-1",
                "trust_class": "observed",
                "labels": ["probe"],
                "retention_class": "standard",
            },
        )
        check(
            "seed: ingest completion claim event (idempotent)",
            r.status_code in (200, 201, 409),
            f"{r.status_code} {r.text[:160]}",
        )
        ev_id = r.json().get("id") if r.status_code < 300 else None
        if ev_id is None:
            # a DUPLICATE_EVENT names the existing record in prose, not as a field
            message = r.json().get("message", "")
            if "record " in message:
                ev_id = message.split("record ", 1)[1].split(";", 1)[0].strip()
        check("seed: the claim event exists", bool(ev_id), str(ev_id))

        # run the pack -> C-001 should raise a finding (no linked verification evidence)
        r = c.post(f"{BASE}/critics/run", json={"event_id": ev_id})
        check("seed: run critics", r.status_code < 300, f"{r.status_code} {r.text[:200]}")
        check(
            "seed: C-001 raised a scoped finding",
            any(
                item["result"] == "finding"
                for item in r.json().get("results", [])
                if item["critic_id"].startswith("c001")
            ),
            str(r.json())[:200],
        )

        findings = c.get(f"{BASE}/findings", params={"limit": 20}).json()["items"]
        check("seed: critics raised at least one finding", bool(findings), f"{len(findings)}")
        finding_id = findings[0]["id"] if findings else None
        print(f"      finding: {finding_id}")

    # --- now read it all back through the MCP stdio session ---
    PROC = subprocess.Popen(
        [sys.executable, "-m", "nyx.conversational.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        send(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "roundtrip-probe", "version": "1.0"},
            },
        )
        send("notifications/initialized", {}, notify=True)

        systems = call("nyx_list_systems", {})["results"]
        ids = [s["id"] for s in systems]
        check(
            "MCP list_systems shows the seeded system",
            sys_id in ids,
            f"{sys_id} in {len(ids)}",
        )

        found = call("nyx_list_findings", {})["results"]
        found_ids = [f["id"] for f in found]
        check(
            "MCP list_findings returns the raised finding",
            finding_id in found_ids,
            f"{finding_id} in {len(found_ids)}",
        )

        detail = call("nyx_get_finding", {"finding_id": finding_id})["result"]
        check(
            "MCP get_finding returns the record the chat would cite",
            detail["id"] == finding_id,
            str(detail)[:160],
        )
        check(
            "finding carries an evidence/provenance link",
            any(k in detail for k in ("evidence_ids", "evidence", "source_event_id")),
            str(sorted(detail.keys())),
        )

        history = call("nyx_get_history", {"finding_id": finding_id})["result"]
        check(
            "MCP get_history returns the dispute record",
            history.get("finding", {}).get("id") == finding_id,
            str(history)[:200],
        )
        # The probe is re-runnable, so this finding may already carry a rebuttal from an earlier
        # run. Record the baseline and assert the delta, not an absolute count.
        baseline = len(history.get("rebuttals", []))
        print(f"      baseline rebuttals: {baseline}")

        # --- a write through MCP: file a rebuttal ---
        reb = call(
            "nyx_submit_rebuttal",
            {
                "finding_id": finding_id,
                "submitted_by": "probe-operator",
                "argument": "The commit was verified by the staging run; the finding is stale.",
                "evidence_ids": [],
                "requested_outcome": "withdrawn",
            },
        )["result"]
        check(
            "MCP submit_rebuttal files the challenge",
            "id" in reb,
            str(reb)[:200],
        )

        history2 = call("nyx_get_history", {"finding_id": finding_id})["result"]
        check(
            "rebuttal appended, history grew by exactly one",
            len(history2.get("rebuttals", [])) == baseline + 1,
            f"{baseline} -> {len(history2.get('rebuttals', []))}",
        )
        check(
            "the original finding row is NOT overwritten (append-only)",
            history2["finding"]["id"] == finding_id
            and history2["finding"]["title"] == history["finding"]["title"]
            and history2["finding"]["reasoning"] == history["finding"]["reasoning"],
            f"{history2['finding']['title'][:60]}",
        )
        check(
            "the recorded rebuttal is the one that was filed",
            history2["rebuttals"][0]["submitted_by"] == "probe-operator",
            str(history2["rebuttals"][0]["submitted_by"]),
        )
        print(
            f"      status: {history['finding']['status']} -> {history2['finding']['status']} "
            "(a rebuttal advances the finding to rebuttal_pending; "
            "that is the lifecycle, not a rewrite)"
        )
        reb_id = history2["rebuttals"][0]["id"]
        print(f"      rebuttal: {reb_id}")

        jobs = call("nyx_list_verification_jobs", {"finding_id": finding_id})["results"]
        check(
            "MCP list_verification_jobs returns an explicit result",
            isinstance(jobs, list),
            str(jobs)[:120],
        )

        r = call(
            "nyx_request_verification",
            {
                "finding_id": finding_id,
                "check_id": "critics.rerun-linked",
                "requested_by": "probe-operator",
            },
        )["result"]
        # The tool returns the created job (or an error). Either is a legitimate outcome; a
        # crash is not.
        check(
            "MCP request_verification queues or refuses (never crashes)",
            isinstance(r, dict) and ("job" in r or "id" in r or "error" in r),
            str(r)[:200],
        )
        if "job" in r:
            print(f"      queued job: {r['job']['id']} ({r['job']['check_id']})")

    finally:
        PROC.stdin.close()
        try:
            PROC.wait(timeout=10)
        except subprocess.TimeoutExpired:
            PROC.kill()

    print()
    if failures:
        print(f"PROBE FAILED: {len(failures)} -> {failures}")
        sys.exit(1)
    print("PROBE PASSED: real finding round-tripped through MCP, rebuttal appended")


if __name__ == "__main__":
    main()
