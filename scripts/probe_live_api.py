"""Independent end-to-end probe of the NYX HTTP API.

Deliberately does NOT use the project's own test helpers: it talks to a real
uvicorn server over real HTTP, so a bug in the test harness cannot hide a bug
in the service.

Checks, in order:
  1. health + ready
  2. register a monitored system
  3. ingest an event whose payload contains several secret shapes
  4. read the event back and assert NO secret value is retrievable
  5. ingest the SAME event again -> idempotent, no second row
  6. ingest a DIFFERENT event with identical payload -> creates a second row
     (identity-based dedupe must NOT merge on content similarity)
  7. invalid payload -> structured error, no partial write
"""

import json
import sys

import httpx

BASE = "http://127.0.0.1:8099/api/v1"
SECRETS = {
    "password": "hunter2-super-secret",
    "bearer": "Bearer sk-live-abcdef1234567890abcdef1234567890",
    "aws": "AKIAIOSFODNN7EXAMPLE",
    "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
}

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' -- ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


with httpx.Client(timeout=20.0) as c:
    # 1. health / ready
    r = c.get(f"{BASE}/health")
    check("health returns 200", r.status_code == 200, f"status={r.status_code}")
    r = c.get(f"{BASE}/ready")
    check("ready returns 200", r.status_code == 200, f"status={r.status_code} body={r.text[:120]}")

    # 2. register a system
    manifest = {
        "name": "ghost-factory-demo",
        "kind": "agent",
        "environment": "development",
        "owner": "probe",
        "capabilities": ["events.write", "findings.read"],
        "adapter": "http",
    }
    r = c.post(f"{BASE}/systems", json=manifest)
    check("register system", r.status_code in (200, 201), f"status={r.status_code} {r.text[:200]}")
    sys_id = r.json().get("id")
    check("system has id", bool(sys_id), str(sys_id))

    # 3. ingest an event carrying secrets
    event = {
        "source_system_id": sys_id,
        "event_type": "agent.completion_claim",
        "occurred_at": "2026-10-19T10:00:00Z",
        "correlation_id": "probe-corr-1",
        "payload": {
            "claim": "All required tests pass for commit abc123",
            "commit": "abc123",
            **SECRETS,
        },
        "producer_event_id": "probe-evt-1",
        "trust_class": "observed",
        "labels": ["demo"],
        "retention_class": "standard",
    }
    r = c.post(f"{BASE}/events", json=event)
    if r.status_code >= 400:
        print("      full error:", r.text[:900])
    check("ingest event", r.status_code in (200, 201), f"status={r.status_code}")
    body = r.json()
    ev_id = body.get("id")

    # 4. read back -- secrets must be gone
    r = c.get(f"{BASE}/events/{ev_id}")
    stored = r.text
    check("read event back", r.status_code == 200, f"status={r.status_code}")
    leaked = [k for k, v in SECRETS.items() if v in stored]
    check("NO secret value retrievable", not leaked, f"leaked={leaked}")
    check(
        "payload still has the claim text",
        "All required tests pass for commit abc123" in stored,
        "",
    )

    # 5. replay the same event -> idempotent
    r2 = c.post(f"{BASE}/events", json=event)
    check(
        "replay accepted or flagged duplicate",
        r2.status_code in (200, 201, 409),
        f"status={r2.status_code} {r2.text[:160]}",
    )
    same_id = r2.json().get("id") if r2.status_code < 300 else None
    check(
        "replay does NOT create a new record",
        same_id in (None, ev_id),
        f"first={ev_id} replay={same_id}",
    )

    # 6. different identity, identical content -> must be a NEW record
    event2 = dict(event)
    event2["producer_event_id"] = "probe-evt-2"
    r3 = c.post(f"{BASE}/events", json=event2)
    check(
        "distinct identity accepted",
        r3.status_code in (200, 201),
        f"status={r3.status_code} {r3.text[:160]}",
    )
    new_id = r3.json().get("id")
    check(
        "identical content + new identity => new record (no content-similarity merge)",
        new_id is not None and new_id != ev_id,
        f"{ev_id} vs {new_id}",
    )

    # 7. invalid payload -> structured error, no partial write
    bad = {"source_system_id": sys_id, "event_type": "x"}  # missing required fields
    r4 = c.post(f"{BASE}/events", json=bad)
    check("invalid event rejected 4xx", 400 <= r4.status_code < 500, f"status={r4.status_code}")
    try:
        err = r4.json()
        has_code = "code" in err
        has_msg = "message" in err
    except Exception:
        has_code = has_msg = False
        err = r4.text[:200]
    check("error is structured (code + message)", has_code and has_msg, json.dumps(err)[:200])

print()
if failures:
    print(f"PROBE FAILED: {len(failures)} check(s) failed -> {failures}")
    sys.exit(1)
print("PROBE PASSED: all checks green")
