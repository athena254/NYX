"""Does the whole-pack critic run fail on a CLEAN store? Isolates C-010 from dirty data."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "D:/NYX/src")

from fastapi.testclient import TestClient  # noqa: E402

from nyx.api.app import create_app  # noqa: E402
from nyx.config import Settings  # noqa: E402

d = Path(tempfile.mkdtemp())
db = d / "probe.sqlite3"
env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NYX_")}
env["NYX_DATABASE_URL"] = f"sqlite:///{db.as_posix()}"
env["NYX_DATA_DIR"] = str(d)
subprocess.run(
    [sys.executable, "-m", "alembic", "upgrade", "head"],
    cwd="D:/NYX",
    env=env,
    capture_output=True,
    text=True,
    check=True,
)

s = Settings(database_url=f"sqlite:///{db.as_posix()}", data_dir=d)
app = create_app(s)
with TestClient(app) as c:
    B = "/api/v1"
    sys_id = c.post(
        f"{B}/systems",
        json={
            "name": "packprobe",
            "kind": "agent",
            "environment": "development",
            "owner": "t",
            "adapter": "http",
        },
    ).json()["id"]
    claim_id = c.post(
        f"{B}/claims",
        json={
            "statement": "Tests pass for commit aaa111.",
            "claimant_system_id": sys_id,
            "claim_type": "completion",
            "scope": {"system_id": sys_id, "artifact": "p", "commit": "aaa111"},
            "producer_event_id": "pp-claim-1",
        },
    ).json()["id"]
    ev = c.post(
        f"{B}/events",
        json={
            "source_system_id": sys_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-20T10:00:00Z",
            "correlation_id": "pp-corr-1",
            "payload": {"claim_id": claim_id},
            "producer_event_id": "pp-evt-1",
            "trust_class": "observed",
            "labels": ["probe"],
            "retention_class": "standard",
        },
    ).json()["id"]
    r = c.post(f"{B}/critics/run", json={"event_id": ev})
    print("WHOLE PACK status:", r.status_code)
    if r.status_code >= 400:
        print("  body:", r.text[:400])
    else:
        for item in r.json()["results"]:
            print("  ", item["critic_id"], item["result"], item.get("finding_id") or "-")
