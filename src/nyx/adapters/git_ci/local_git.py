"""A local-git CI provider (phase 5, slice 5.4).

``LocalGitClient`` presents the same surface as
:class:`nyx.adapters.git_ci.github_client.GitHubCiClient` — ``fetch_check_runs`` and
``fetch_actions_runs`` returning ``list[dict[str, Any]]`` — but reads a **local repository**
instead of the GitHub API. That is the whole argument for the adapter boundary: the provider is
swappable, so ``normalize.py`` does not change and neither does the adapter.

**How it reads.** The standard library only, through ``pathlib``: ``.git/HEAD``, the loose refs
under ``.git/refs/**``, ``.git/packed-refs``, and the reflog at ``.git/logs/HEAD`` (plus the
per-ref log at ``.git/logs/<ref>`` when git wrote one). No process is started, no command is run,
and no network is touched — which is why this slice needs no allowlist entry in
``tests/security/test_verification_allowlist.py``: the gate that forbids arbitrary command
execution anywhere under ``src/`` is satisfied here by construction, not by review.

> **Object reading.** This provider reads the refs and reflog, and, when the object store is
> readable, the **commit and tree objects** those refs point at (loose and packed, including
> delta-encoded packs; see :mod:`nyx.adapters.git_ci.objects`). A run therefore carries the real
> commit message, author, parents, committer time, and the set of paths changed against the first
> parent. **Textual diffs are not produced**: a path change set is the maximum claim. If an
> object is missing or corrupt (a shallow clone, a pruned store), that commit degrades to the
> reflog's own words rather than inventing contents; the object reader itself fails with a coded
> error, and its tests exercise that path directly.

**What a run dict is.** A local repository has no external run id, so the commit *is* the
external object: ``id`` and ``head_sha`` are the commit sha, and the reflog's subject line is the
``name``. ``conclusion`` is ``None`` — a commit is not a test result, and inventing a pass/fail
word would be fabricating evidence (the rule ``normalize.py`` enforces). ``status`` is
``completed`` because the commit is in the ref's history, which is what makes the record usable.
The two vocabularies ``normalize.py`` reads are both emitted: ``started_at``/``completed_at`` for
check runs, ``run_started_at``/``updated_at`` for Actions runs.

**The repository path is untrusted configuration.** It is validated with
:func:`nyx.ingestion.paths.resolve_within` *before anything is read*, so ``..`` in a configured
path is refused rather than resolved. The locator is relative to ``base_dir`` (the current
directory by default; the orchestrator should pass ``Settings.data_dir``); an absolute locator is
refused by the guard, exactly as the evidence route refuses one.

**Failures.** Every failure is a :class:`~nyx.adapters.git_ci.github_client.GitCiError` carrying
the same stable code the GitHub client uses: a missing repository, a directory with no ``.git``,
or an unknown ref is ``not_found``; a traversal locator refused by the path guard is ``denied``
(the same code the SSRF guard uses, and for the same reason — a deliberate refusal, not an
accident); anything unexpected while reading is ``unavailable``. Nothing here raises a bare
``KeyError``, ``OSError``, or ``PathTraversalError`` to a caller.

This module imports only its own sibling ``config`` / ``github_client`` and the ingestion path
guard, plus the standard library (the adapter boundary, ``docs/architecture.md`` §3). It imports
nothing from ``nyx.critics``, ``nyx.findings``, ``nyx.disputes``, ``nyx.policies``,
``nyx.storage``, ``nyx.retention``, ``nyx.auth``, ``nyx.systems``, or ``nyx.audit``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nyx.adapters.git_ci.config import GitCiConfig
from nyx.adapters.git_ci.github_client import (
    CODE_DENIED,
    CODE_NOT_FOUND,
    CODE_UNAVAILABLE,
    GitCiError,
)
from nyx.adapters.git_ci.objects import Commit, ObjectReader
from nyx.ingestion.paths import PathTraversalError, resolve_within

#: The sha a ref has never pointed at: the old half of the first line of every reflog.
_NULL_SHA = "0" * 40
#: The reflog line git writes: ``<old> <new> <name> <email> <ts> <tz>\t<message>``. The name may
#: contain spaces, so it is matched non-greedily up to the bracketed email rather than split.
_REFLOG_LINE = re.compile(
    r"^(?P<old>[0-9a-f]{40}) (?P<new>[0-9a-f]{40}) "
    r"(?P<name>.*?) <(?P<email>[^>]*)> (?P<ts>\d+) (?P<tz>[+-]\d{4})"
    r"(?:\t(?P<message>.*))?$"
)
#: git's own prefix on a reflog message for a commit: ``commit:`` / ``commit (initial):``. The
#: subject is what follows it, and the subject is the honest "job name" for a commit.
_COMMIT_PREFIX = re.compile(r"^commit(?:\s*\([^)]*\))?:\s*")
#: A symbolic ref file: ``.git/HEAD`` holds ``ref: refs/heads/main``.
_SYMBOLIC_REF = re.compile(r"^ref:\s*(?P<target>refs/[^\s]+)$")
#: A worktree's ``.git`` *file*: ``gitdir: <path>``.
_GITDIR = re.compile(r"^gitdir:\s*(?P<target>.+)$")
#: A sha, full or abbreviated, as git writes one.
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
#: ``normalize.py`` refuses a run name longer than 200 characters. Bound it here, so a long
#: subject line truncates instead of failing the whole fetch.
_MAX_NAME = 200


@dataclass(frozen=True)
class _ReflogEntry:
    """One reflog line: the commit a ref moved to, when, and git's description of the move."""

    new_sha: str
    author: str
    occurred_at: datetime
    message: str


@dataclass(frozen=True)
class _ResolvedRef:
    """A ref resolved to a sha, plus the log this provider reads for it."""

    sha: str
    log_name: str


def _offset(tz: str) -> timezone:
    """Turn git's ``+0300`` into a ``timezone``. A naive timestamp is never produced."""

    sign = -1 if tz.startswith("-") else 1
    return timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])))


def _subject(message: str) -> str:
    """The commit subject from a reflog message: git's ``commit:`` prefix removed.

    A message this reader does not recognise is used as it stands rather than dropped — the
    reflog's own words are the most honest name available.
    """

    text = _COMMIT_PREFIX.sub("", message, count=1).strip()
    if not text:
        text = message.strip()
    return _truncate(text, _MAX_NAME)


def _truncate(text: str, limit: int) -> str:
    """Bound one line of untrusted text; git permits arbitrarily long subjects."""

    text = text.strip()
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _commit_subject(message: str) -> str:
    """The first line of a real commit message, bounded for the run's ``name``."""

    first = message.strip().splitlines()
    return _truncate(first[0], _MAX_NAME) if first else ""


class LocalGitClient:
    """Same interface as ``GitHubCiClient``, backed by a local ``.git`` directory.

    ``repository_path`` is a locator relative to ``base_dir`` and is validated by the path guard
    before anything is read; ``base_dir`` defaults to the current directory. The configured
    repository is read once per fetch, so a repository that changes between fetches is observed
    again rather than cached.
    """

    def __init__(
        self,
        config: GitCiConfig,
        *,
        repository_path: str | None = None,
        base_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._last_truncated = 0

        base = Path(base_dir) if base_dir is not None else Path.cwd()
        locator = repository_path if repository_path else "."
        try:
            resolved = resolve_within(base, locator)
        except PathTraversalError as exc:
            # Refused before any read: the guard is the control, and the caller gets the same
            # stable code the SSRF guard in `github_client` returns for a refused target.
            raise GitCiError(
                f"local-git repository locator {locator!r} was refused by the path guard",
                code=CODE_DENIED,
            ) from exc
        self._repository = resolved

    # -- public surface ----------------------------------------------------

    @property
    def last_truncated(self) -> int:
        """How many records the most recent fetch dropped. ``0`` when nothing was dropped."""

        return self._last_truncated

    def close(self) -> None:
        """Interface parity with ``GitHubCiClient``; there is no connection to release."""

        return None

    def fetch_check_runs(self, *, ref: str) -> list[dict[str, Any]]:
        """The commits ``ref`` has pointed at, in the check-run vocabulary ``normalize`` reads."""

        return self._fetch(ref, kind="check")

    def fetch_actions_runs(self, *, ref: str) -> list[dict[str, Any]]:
        """The same commits in the Actions-run vocabulary (``run_started_at``/``updated_at``)."""

        return self._fetch(ref, kind="actions")

    # -- internals ---------------------------------------------------------

    def _fetch(self, ref: str, *, kind: str) -> list[dict[str, Any]]:
        resolved = self._resolve_ref(ref)
        reader = self._object_reader()
        runs = [self._run(commit, kind=kind, reader=reader) for commit in self._commits(resolved)]
        limit = self._config.max_runs_per_fetch
        self._last_truncated = max(0, len(runs) - limit)
        return runs[:limit]

    def _object_reader(self) -> ObjectReader | None:
        """An object reader for this repository, or ``None`` when the store is unreadable.

        A missing ``.git`` is already a ``not_found`` from ``_resolve_ref``; this is only the
        shallow/pruned/corrupt case, where the reflog is still usable evidence.
        """

        try:
            return ObjectReader(self._require_git_dir())
        except GitCiError:
            return None

    def _run(
        self, commit: _ReflogEntry, *, kind: str, reader: ObjectReader | None = None
    ) -> dict[str, Any]:
        started = commit.occurred_at.isoformat()
        name = _subject(commit.message)
        author = commit.author
        enriched: dict[str, Any] = {}
        info = self._commit(reader, commit.new_sha)
        if info is not None:
            started = info.committed_at.isoformat()
            name = _commit_subject(info.message) or name
            author = info.author_name
            enriched = {
                "message": _truncate(info.message, 4000),
                "parents": list(info.parents),
                "files": self._changed_files(reader, info.sha),
            }
        run: dict[str, Any] = {
            # The commit is the external object: its sha is the id, so the producer key is
            # stable across refetches and two commits can never collide.
            "id": commit.new_sha,
            "head_sha": commit.new_sha,
            "name": name,
            "author": author,
            "status": "completed",
            # A commit is not a test result. No conclusion is invented.
            "conclusion": None,
            "html_url": self._file_url(commit.new_sha),
        }
        run.update(enriched)
        if kind == "check":
            run["started_at"] = started
            run["completed_at"] = started
        else:
            run["run_started_at"] = started
            run["updated_at"] = started
        return run

    @staticmethod
    def _commit(reader: ObjectReader | None, sha: str) -> Commit | None:
        """The parsed commit, or ``None`` when this repository cannot supply its objects."""

        if reader is None:
            return None
        try:
            return reader.commit(sha)
        except GitCiError:
            return None

    @staticmethod
    def _changed_files(reader: ObjectReader | None, sha: str) -> list[str]:
        if reader is None:
            return []
        try:
            return reader.changed_paths(sha)
        except GitCiError:
            return []

    def _file_url(self, sha: str) -> str:
        """A plausible ``file://`` reference for a local commit, stored as ``source_ref``."""

        try:
            return f"{self._repository.as_uri()}#commit={sha}"
        except ValueError:
            return f"file://{self._repository.as_posix()}#commit={sha}"

    # --- locating the repository ------------------------------------------

    def _require_git_dir(self) -> Path:
        """The ``.git`` directory of the configured repository, or ``not_found``.

        A worktree or submodule keeps ``.git`` as a *file* naming the real git directory; that is
        followed, because the refs and the reflog live there.
        """

        git = self._repository / ".git"
        if git.is_dir():
            return git
        if git.is_file():
            target = self._gitdir_from_file(git)
            if target is not None:
                return target
        raise GitCiError(f"no .git directory in {self._repository.as_posix()}", code=CODE_NOT_FOUND)

    def _gitdir_from_file(self, gitfile: Path) -> Path | None:
        match = _GITDIR.match(self._read_text(gitfile))
        if match is None:
            return None
        target = Path(match.group("target").strip())
        return target if target.is_absolute() else self._repository / target

    # --- refs -------------------------------------------------------------

    def _resolve_ref(self, ref: str) -> _ResolvedRef:
        """Resolve ``ref`` to a sha and the reflog this provider reads for it.

        A branch name, a full ref name, a tag, and a sha the ref has actually pointed at are all
        accepted. Anything else is ``not_found``: this provider does not guess a commit from a
        name it cannot see.
        """

        if not ref.strip():
            raise GitCiError("ref must be a non-empty string", code=CODE_NOT_FOUND)

        refs = self._refs()
        for candidate in (ref, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/remotes/{ref}"):
            sha = refs.get(candidate)
            if sha is not None and _SHA.match(sha):
                return _ResolvedRef(sha=sha, log_name=candidate)

        if _SHA.match(ref):
            for entry in self._reflog_entries("HEAD"):
                if entry.new_sha == ref or entry.new_sha.startswith(ref):
                    return _ResolvedRef(sha=entry.new_sha, log_name="HEAD")

        raise GitCiError(f"ref {ref!r} is not a ref in this repository", code=CODE_NOT_FOUND)

    def _refs(self) -> dict[str, str]:
        """Every ref in the repository — loose, packed, and ``HEAD`` — dereferenced to a sha."""

        git = self._require_git_dir()
        raw: dict[str, str] = {"HEAD": self._read_text(git / "HEAD")}

        refs_root = git / "refs"
        if refs_root.is_dir():
            for path in sorted(refs_root.rglob("*")):
                if path.is_file():
                    name = f"refs/{path.relative_to(refs_root).as_posix()}"
                    raw[name] = self._read_text(path)

        packed = git / "packed-refs"
        if packed.is_file():
            for line in self._read_text(packed).splitlines():
                entry = line.strip()
                # `# pack-refs with: ...` is a header; `^<sha>` is a peeled tag target.
                if not entry or entry.startswith(("#", "^")):
                    continue
                sha, _, name = entry.partition(" ")
                if len(sha) == 40 and name.strip():
                    raw[name.strip()] = sha

        return {name: self._deref(raw, value, frozenset()) for name, value in raw.items()}

    @staticmethod
    def _deref(raw: dict[str, str], value: str, seen: frozenset[str]) -> str:
        """Follow a symbolic ref one level at a time; return the sha it names."""

        match = _SYMBOLIC_REF.match(value)
        if match is None or value in seen:
            return value
        target = match.group("target")
        if target not in raw:
            return value
        return LocalGitClient._deref(raw, raw[target], seen | {value})

    # --- the reflog -------------------------------------------------------

    def _commits(self, resolved: _ResolvedRef) -> list[_ReflogEntry]:
        """The commits ``resolved.sha`` and its history, newest first.

        The reflog is read from the first entry that names the resolved sha to the end of the
        file. For a branch that is the branch's whole history; for a tag or an explicit sha it is
        the history up to that commit, and commits after it are correctly excluded. A resolved
        sha that appears in no reflog yields no commits rather than an invented one.
        """

        commits: list[_ReflogEntry] = []
        seen: set[str] = set()
        started = False
        for entry in self._reflog_entries(resolved.log_name):
            if not started:
                if entry.new_sha != resolved.sha:
                    continue
                started = True
            if entry.new_sha == _NULL_SHA or entry.new_sha in seen:
                continue
            seen.add(entry.new_sha)
            commits.append(entry)
        return commits

    def _reflog_entries(self, log_name: str) -> list[_ReflogEntry]:
        """Parse the ref's own reflog, falling back to ``logs/HEAD`` when it wrote none."""

        git = self._require_git_dir()
        candidates = [] if log_name == "HEAD" else [git / "logs" / Path(log_name)]
        candidates.append(git / "logs" / "HEAD")
        for path in candidates:
            if path.is_file():
                return self._parse_reflog(path)
        return []

    def _parse_reflog(self, path: Path) -> list[_ReflogEntry]:
        """Parse a reflog, newest first. A line this reader does not understand is skipped."""

        entries: list[_ReflogEntry] = []
        for line in self._read_text(path).splitlines():
            match = _REFLOG_LINE.match(line.strip())
            if match is None:
                continue
            entries.append(
                _ReflogEntry(
                    new_sha=match.group("new"),
                    author=match.group("name"),
                    occurred_at=datetime.fromtimestamp(
                        int(match.group("ts")),
                        tz=_offset(match.group("tz")),
                    ),
                    message=(match.group("message") or "").strip(),
                )
            )
        return entries

    # --- reading ----------------------------------------------------------

    def _read_text(self, path: Path) -> str:
        """Read a repository file as text, or fail as ``unavailable`` — never as a traceback."""

        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            try:
                location = path.relative_to(self._repository).as_posix()
            except ValueError:
                location = path.name
            raise GitCiError(
                f"could not read {location} in the local repository", code=CODE_UNAVAILABLE
            ) from exc


__all__ = ["LocalGitClient"]
