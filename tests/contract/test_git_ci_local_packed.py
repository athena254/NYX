"""Contract test: a packed local repository's output is accepted by the real gateway (H2b).

The unit tests prove the object decoder; this test proves the boundary. It creates a packed
repository with the real git binary, runs the **unchanged** ``LocalGitClient`` and ``normalize``,
and pushes the result through the real ``IngestionService`` against a migrated database. The
re-fetch is a duplicate, and nothing produces a finding.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from nyx.adapters.git_ci.config import GitCiConfig
from nyx.adapters.git_ci.local_git import LocalGitClient
from nyx.adapters.git_ci.normalize import normalize_check_run
from nyx.findings.service import FindingQuery, FindingService
from nyx.ingestion.inputs import EventInput, EvidenceInput
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory

GIT = shutil.which("git") or ""
pytestmark = pytest.mark.skipif(not GIT, reason="the git binary is not available")

REPOSITORY = "owner/packed"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixture creation only; `src/` never runs a command
        [GIT, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _packed_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Contract Author")
    _git(path, "config", "user.email", "contract@example.invalid")
    for index in range(4):
        (path / "build.txt").write_text(f"build {index}\n", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-m", f"Build {index}")
    _git(path, "repack", "-adf", "--depth=50", "--window=250")
    _git(path, "prune-packed")
    return path


def _config() -> GitCiConfig:
    return GitCiConfig(
        provider="local_git",
        api_base_url="file:///local",
        repository=REPOSITORY,
        max_runs_per_fetch=10,
    )


def test_a_packed_local_run_is_accepted_and_deduplicated(
    tmp_path: Path, service: IngestionService, session_factory: SessionFactory
) -> None:
    _packed_repository(tmp_path / "repo")
    client = LocalGitClient(_config(), repository_path="repo", base_dir=tmp_path)
    run = client.fetch_check_runs(ref="main")[0]

    records = normalize_check_run(run, repository=REPOSITORY)
    event = next(item for item in records if isinstance(item, EventInput))
    evidence = next(item for item in records if isinstance(item, EvidenceInput))

    first = service.ingest_event(event, request_id="packed-event")
    assert first.status == "created", first.error
    second = service.ingest_event(event, request_id="packed-event-again")
    assert second.status == "duplicate"
    assert second.record_id == first.record_id

    stored = service.register_evidence(
        evidence.model_dump(mode="json"), request_id="packed-evidence"
    )
    assert stored.status == "created", stored.error

    assert run["files"] == ["build.txt"]
    assert run["parents"]
    assert run["conclusion"] is None

    assert FindingService(session_factory).list_findings(FindingQuery()).items == []
