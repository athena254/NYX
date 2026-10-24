"""Loose-object tests for the local-git object reader (slice H2a).

The repositories here are created with the real ``git`` binary inside ``tests/`` â€” the
subprocess gate covers ``src/`` only, and a hand-built object store would test this reader
against itself rather than against git.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

from nyx.adapters.git_ci.config import GitCiConfig
from nyx.adapters.git_ci.github_client import GitCiError
from nyx.adapters.git_ci.local_git import LocalGitClient
from nyx.adapters.git_ci.objects import ObjectReader

GIT = shutil.which("git") or ""
pytestmark = pytest.mark.skipif(not GIT, reason="the git binary is not available")

AUTHOR = "Object Author"
EMAIL = "objects@example.invalid"


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


def _repository(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", AUTHOR)
    _git(path, "config", "user.email", EMAIL)
    return path


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for name, text in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _writable(path: Path) -> None:
    """Git marks object files read-only on Windows; the tests mutate copies on purpose."""

    path.chmod(0o666)


def test_loose_commit_metadata_and_tree_listing(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    first = _commit(repo, "Initial commit", {"README.md": "hello\n", "src/app.py": "print(1)\n"})
    second = _commit(repo, "Change app", {"src/app.py": "print(2)\n"})

    reader = ObjectReader(repo / ".git")
    info = reader.commit(second)

    assert info.sha == second
    assert info.parents == (first,)
    assert info.author_name == AUTHOR
    assert info.author_email == EMAIL
    assert info.message.startswith("Change app")
    assert info.committed_at.tzinfo is not None
    assert info.tree

    assert reader.list_files(info.tree) == ["README.md", "src/app.py"]
    assert reader.changed_paths(second) == ["src/app.py"]
    # A root commit has no parent, so every file is new.
    assert reader.changed_paths(first) == ["README.md", "src/app.py"]

    entries = reader.tree(info.tree)
    assert {entry.name for entry in entries} == {"README.md", "src"}
    assert next(entry for entry in entries if entry.name == "src").is_tree


def test_local_git_client_enriches_a_run_from_objects(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    _commit(repo, "Initial commit", {"README.md": "hello\n", "a.txt": "a\n"})
    _commit(repo, "Touch a", {"a.txt": "changed\n"})

    client = LocalGitClient(
        _config(),
        repository_path="repo",
        base_dir=tmp_path,
    )
    run = client.fetch_check_runs(ref="main")[0]

    assert run["conclusion"] is None
    assert run["name"] == "Touch a"
    assert run["author"] == AUTHOR
    assert run["files"] == ["a.txt"]
    assert run["parents"]
    assert "Touch a" in run["message"]


def test_a_missing_object_is_not_found(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    _commit(repo, "Initial commit", {"a.txt": "a\n"})
    reader = ObjectReader(repo / ".git")

    with pytest.raises(GitCiError) as excinfo:
        reader.read("0" * 40)

    assert excinfo.value.code == "not_found"


def test_a_corrupt_loose_object_is_unavailable_not_a_traceback(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    sha = _commit(repo, "Initial commit", {"a.txt": "a\n"})
    path = repo / ".git" / "objects" / sha[:2] / sha[2:]
    assert path.is_file()
    _writable(path)
    path.write_bytes(b"this is not zlib")

    with pytest.raises(GitCiError) as excinfo:
        ObjectReader(repo / ".git").read(sha)

    assert excinfo.value.code == "unavailable"


def test_an_object_that_does_not_hash_to_its_name_is_refused(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    sha = _commit(repo, "Initial commit", {"a.txt": "a\n"})
    path = repo / ".git" / "objects" / sha[:2] / sha[2:]

    other = b"blob 4\0nope"
    _writable(path)
    path.write_bytes(zlib.compress(other))
    assert hashlib.sha1(other).hexdigest() != sha  # noqa: S324 - reproducing git's object id

    with pytest.raises(GitCiError) as excinfo:
        ObjectReader(repo / ".git").read(sha)

    assert excinfo.value.code == "unavailable"


def test_commit_refuses_a_non_commit_object(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    sha = _commit(repo, "Initial commit", {"a.txt": "a\n"})
    reader = ObjectReader(repo / ".git")
    info = reader.commit(sha)
    blob_sha = next(entry for entry in reader.tree(info.tree) if entry.name == "a.txt").sha

    with pytest.raises(GitCiError) as excinfo:
        reader.commit(blob_sha)

    assert excinfo.value.code == "unavailable"


def _config() -> GitCiConfig:
    return GitCiConfig(
        provider="local_git",
        api_base_url="file:///local",
        repository="owner/repo",
        max_runs_per_fetch=10,
    )
