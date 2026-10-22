"""Unit tests for the local-git provider (Phase 5, slice 5.4).

The provider is the same adapter contract as ``GitHubCiClient`` over a local repository, so these
tests assert three things: that a real ``.git`` directory is read correctly, that its output is
acceptable to the *unchanged* ``normalize.py``, and that the two properties the slice exists for
hold — the repository path is guarded, and nothing under ``src/`` can execute a command.

The repository in the positive tests is hand-built with ``pathlib``, in the exact plain-text form
git writes (``.git/HEAD``, ``.git/refs/heads/main``, ``.git/logs/HEAD``), so the tests need no git
binary and are deterministic. One extra test, skipped when ``git`` is absent, builds a repository
with the real binary to prove the hand-built fixture matches what git actually writes.

Per the plan's warning, no test here reaches for a command line on the provider's behalf: the
``git`` binary is used only to *create* a fixture, and only in ``tests/`` — ``src/`` is banned.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from nyx.adapters.git_ci.config import GitCiConfig
from nyx.adapters.git_ci.github_client import GitCiError
from nyx.adapters.git_ci.local_git import LocalGitClient
from nyx.adapters.git_ci.normalize import (
    EVENT_TYPE_CHECK,
    EVENT_TYPE_WORKFLOW,
    normalize_actions_run,
    normalize_check_run,
)
from nyx.ingestion.inputs import EventInput, EvidenceInput
from nyx.ingestion.paths import PathTraversalError, resolve_within

REPOSITORY = "owner/repo"
SHA = "0123456789abcdef0123456789abcdef01234567"
OLD_SHA = "fedcba9876543210fedcba9876543210fedcba98"
NULL_SHA = "0" * 40
SUBJECT = "Fix the release pipeline"
INITIAL_SUBJECT = "Initial commit"
AUTHOR = "Test Author"
EMAIL = "author@example.invalid"
TIMESTAMP = 1_792_625_907
TIMEZONE = "+0300"

MODULE = (
    Path(__file__).resolve().parents[2] / "src" / "nyx" / "adapters" / "git_ci" / "local_git.py"
)

#: Patterns that would let a repository path or a payload reach a command line. ``src/`` must
#: contain none of them, and this module is the one slice that could have tripped the gate.
FORBIDDEN_PATTERNS = ("subprocess", "os.system", "shell=True", "eval(", "exec(")


def _config(**overrides: object) -> GitCiConfig:
    values: dict[str, object] = {
        "provider": "local_git",
        "api_base_url": "file:///local",
        "repository": REPOSITORY,
        "max_runs_per_fetch": 10,
    }
    values.update(overrides)
    return GitCiConfig(**values)


def _client(base: Path, locator: str, **overrides: object) -> LocalGitClient:
    return LocalGitClient(_config(**overrides), repository_path=locator, base_dir=base)


def _reflog_line(old: str, new: str, message: str, *, timestamp: int = TIMESTAMP) -> str:
    """One reflog line, in the exact format git writes it."""

    return f"{old} {new} {AUTHOR} <{EMAIL}> {timestamp} {TIMEZONE}\t{message}\n"


def _make_repository(path: Path) -> Path:
    """Hand-build a ``.git`` directory in the plain-text form git writes.

    Two commits are on ``main``: ``OLD_SHA`` was the initial commit and ``SHA`` is the tip, with
    a ``v1.0`` tag packed at ``OLD_SHA`` so the packed-refs reader is exercised too.
    """

    git = path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "logs" / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text(f"{SHA}\n", encoding="utf-8")
    (git / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted \n{OLD_SHA} refs/tags/v1.0\n",
        encoding="utf-8",
    )
    reflog = _reflog_line(OLD_SHA, SHA, f"commit: {SUBJECT}") + _reflog_line(
        NULL_SHA, OLD_SHA, f"commit (initial): {INITIAL_SUBJECT}", timestamp=TIMESTAMP - 3600
    )
    (git / "logs" / "HEAD").write_text(reflog, encoding="utf-8")
    (git / "logs" / "refs" / "heads" / "main").write_text(reflog, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- positive


def test_the_provider_returns_the_commit_as_a_run_shaped_dict(tmp_path: Path) -> None:
    _make_repository(tmp_path / "repo")
    client = _client(tmp_path, "repo")

    runs = client.fetch_check_runs(ref="main")

    # The reflog is newest-first: the tip first, the initial commit second.
    assert [run["head_sha"] for run in runs] == [SHA, OLD_SHA]
    assert [run["id"] for run in runs] == [SHA, OLD_SHA]

    run = runs[0]
    assert run["name"] == SUBJECT
    assert run["status"] == "completed"
    # A commit is not a test result: no conclusion is invented.
    assert run["conclusion"] is None
    assert run["started_at"] and run["completed_at"]
    assert datetime.fromisoformat(run["started_at"]).tzinfo is not None
    # `html_url` is stored as the evidence `source_ref`, so it must be a plausible reference.
    assert run["html_url"].startswith("file://")
    assert run["html_url"].endswith(f"#commit={SHA}")

    # The Actions vocabulary is the same commits under the other timestamp names.
    actions = client.fetch_actions_runs(ref="main")
    assert [run["head_sha"] for run in actions] == [SHA, OLD_SHA]
    assert actions[0]["run_started_at"] and actions[0]["updated_at"]
    assert "started_at" not in actions[0]

    # A full ref name resolves too, and a tag is read from packed-refs.
    assert client.fetch_check_runs(ref="refs/heads/main")[0]["head_sha"] == SHA
    assert client.fetch_check_runs(ref="v1.0")[0]["head_sha"] == OLD_SHA

    # Nothing was dropped.
    assert client.last_truncated == 0


def test_normalize_produces_the_same_shape_as_the_github_path(tmp_path: Path) -> None:
    _make_repository(tmp_path / "repo")
    client = _client(tmp_path, "repo")
    run = client.fetch_check_runs(ref="main")[0]

    local_records = normalize_check_run(run, repository=REPOSITORY)

    # The one field a provider legitimately owns is the URL; the expected value is computed here
    # from the same guard the provider uses, not read back out of the provider.
    expected_url = f"{resolve_within(tmp_path, 'repo').as_uri()}#commit={SHA}"
    assert run["html_url"] == expected_url

    github_run = {
        "id": SHA,
        "head_sha": SHA,
        "name": SUBJECT,
        "status": "completed",
        "conclusion": None,
        "html_url": expected_url,
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
    }
    github_records = normalize_check_run(github_run, repository=REPOSITORY)

    # Field-by-field equality with the GitHub path's output for an equivalent run.
    assert [r.model_dump(mode="json") for r in local_records] == [
        r.model_dump(mode="json") for r in github_records
    ]

    event = next(r for r in local_records if isinstance(r, EventInput))
    evidence = next(r for r in local_records if isinstance(r, EvidenceInput))
    assert event.event_type == EVENT_TYPE_CHECK
    assert event.payload["commit"] == SHA
    assert event.trust_class.value == "observed"
    assert event.producer_event_id == f"gh:{REPOSITORY}:check:{SHA}"
    assert evidence.kind.value == "ci_test_result"
    assert evidence.scope is not None and evidence.scope.commit == SHA
    assert evidence.source_ref == expected_url
    assert evidence.source_ref.startswith("file://")

    # The Actions vocabulary lands on the workflow event type, same shape.
    actions_records = normalize_actions_run(
        client.fetch_actions_runs(ref="main")[0], repository=REPOSITORY
    )
    actions_event = next(r for r in actions_records if isinstance(r, EventInput))
    assert actions_event.event_type == EVENT_TYPE_WORKFLOW
    assert actions_event.payload["commit"] == SHA


# --------------------------------------------------------------------------- negative


def test_a_repository_that_does_not_exist_is_not_found_not_a_traceback(tmp_path: Path) -> None:
    with pytest.raises(GitCiError) as excinfo:
        _client(tmp_path, "does-not-exist").fetch_check_runs(ref="main")

    assert excinfo.value.code == "not_found"
    assert isinstance(excinfo.value, GitCiError)

    with pytest.raises(GitCiError) as actions:
        _client(tmp_path, "does-not-exist").fetch_actions_runs(ref="main")
    assert actions.value.code == "not_found"


def test_a_traversal_locator_is_refused_by_the_path_guard(tmp_path: Path) -> None:
    # The decoy is a *valid* repository outside the base. If the locator were read instead of
    # guarded, this call would succeed and return the decoy's commit, so the refusal below can
    # only have come from the guard.
    decoy = _make_repository(tmp_path / "decoy")
    assert (decoy / ".git" / "HEAD").is_file()

    with pytest.raises(GitCiError) as excinfo:
        _client(tmp_path / "work", "../decoy").fetch_check_runs(ref="main")

    assert excinfo.value.code == "denied"
    assert isinstance(excinfo.value.__cause__, PathTraversalError)
    assert SHA not in str(excinfo.value)


def test_a_directory_without_dot_git_is_not_found(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "README.md").write_text("not a repository\n", encoding="utf-8")

    with pytest.raises(GitCiError) as excinfo:
        _client(tmp_path, "plain").fetch_check_runs(ref="main")

    assert excinfo.value.code == "not_found"
    assert isinstance(excinfo.value, GitCiError)


def test_an_unknown_ref_is_not_found(tmp_path: Path) -> None:
    _make_repository(tmp_path / "repo")

    with pytest.raises(GitCiError) as excinfo:
        _client(tmp_path, "repo").fetch_check_runs(ref="no-such-branch")

    assert excinfo.value.code == "not_found"


# --------------------------------------------------------------------------- boundary


def test_the_same_commit_yields_the_same_producer_event_id(tmp_path: Path) -> None:
    _make_repository(tmp_path / "repo")

    # Two separately constructed clients: the key must be a property of the commit, not of the
    # client's cached state, so a refetch is a duplicate rather than a second record.
    first = normalize_check_run(
        _client(tmp_path, "repo").fetch_check_runs(ref="main")[0], repository=REPOSITORY
    )
    second = normalize_check_run(
        _client(tmp_path, "repo").fetch_check_runs(ref="main")[0], repository=REPOSITORY
    )

    assert [r.producer_event_id for r in first] == [r.producer_event_id for r in second]
    assert first[0].producer_event_id == f"gh:{REPOSITORY}:check:{SHA}"
    assert first[1].producer_event_id == f"gh:{REPOSITORY}:check-evidence:{SHA}"

    # A different commit produces a different key, so two commits cannot collide.
    other = normalize_check_run(
        _client(tmp_path, "repo").fetch_check_runs(ref="main")[1], repository=REPOSITORY
    )
    assert other[0].producer_event_id == f"gh:{REPOSITORY}:check:{OLD_SHA}"
    assert other[0].producer_event_id != first[0].producer_event_id


def test_the_module_source_contains_no_arbitrary_execution() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert source.strip()

    for forbidden in FORBIDDEN_PATTERNS:
        assert forbidden not in source, f"{forbidden!r} appears in {MODULE.name}"

    # The text scan is what the build gate runs; the import scan is the stronger form of the same
    # claim, and it is what would actually matter if a word appeared inside a string.
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(name == "subprocess" or name.startswith("subprocess.") for name in imported)


# --------------------------------------------------------------------------- real git


GIT = shutil.which("git")


@pytest.mark.skipif(GIT is None, reason="the git binary is not available on this machine")
def test_a_repository_created_by_the_git_binary(tmp_path: Path) -> None:
    """The hand-built fixture above must match what git itself writes."""

    repo = tmp_path / "real"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603 - test fixture creation only; `src/` never runs a command
            [GIT, *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-b", "main")
    # `git init` writes no reflog; the commit does. That is why the fixture has one.
    git(
        "-c",
        f"user.name={AUTHOR}",
        "-c",
        f"user.email={EMAIL}",
        "commit",
        "--allow-empty",
        "-m",
        SUBJECT,
    )

    client = _client(tmp_path, "real")
    runs = client.fetch_check_runs(ref="main")

    sha = (repo / ".git" / "refs" / "heads" / "main").read_text(encoding="utf-8").strip()
    assert len(runs) == 1
    assert runs[0]["head_sha"] == sha
    assert runs[0]["id"] == sha
    # git's `commit (initial):` prefix is stripped, leaving the subject.
    assert runs[0]["name"] == SUBJECT

    # The timestamp is the commit's own, read from the reflog rather than defaulted.
    assert datetime.fromisoformat(runs[0]["started_at"]).tzinfo is not None

    # And the real repository's output is acceptable to the unchanged normalizer.
    records = normalize_check_run(runs[0], repository=REPOSITORY)
    assert len(records) == 2
    assert all(isinstance(r, (EventInput, EvidenceInput)) for r in records)
