# ADR 0011 — Read local git objects with the standard library

**Status:** accepted, 24 October 2026. Supersedes the object-format limitation recorded in
ADR 0008; the adapter boundary and no-subprocess rule there are unchanged.

## Context

The local-git provider (Phase 5) read `.git/HEAD`, refs, `packed-refs`, and the reflog. That was
enough to produce a run per commit — sha, author, time, subject — but it could not read the
commit message body, parents, tree, or the files a commit changed. The limitation was disclosed
in the module and in ADR 0008, and the Phase 5 dependency review rejected dulwich, GitPython, and
pygit2.

A commit is still not a test result: the provider must not invent a `conclusion`. Reading the
objects adds *observed* facts about the commit, not a verdict.

## Decision

Add `src/nyx/adapters/git_ci/objects.py`, a read-only object reader using only the standard
library:

- **Loose objects** are zlib-deflated files under `objects/<2>/<38>`; the header is parsed and
  the payload's SHA-1 is checked against the id it was read by.
- **Packfiles** use the version-2 `.idx` (fanout, sha table, CRC table, 4-byte offsets with the
  large-offset escape) and the `.pack` (object header varint, zlib stream).
- **Deltas**: both `OFS_DELTA` and `REF_DELTA` are resolved, with the base offset/sha consumed
  *before* the compressed delta stream, and the delta instruction stream applied with bounds
  checks.
- **Scope**: commit metadata (tree, parents, author/committer, times, message), tree listings,
  and a changed-path set against the first parent. **No textual diff is produced.** The
  changed-path set is the maximum claim.
- **Bounds**: object size, pack size, delta depth, and file count are capped; every failure is a
  `GitCiError` with code `unavailable` (corrupt/unsupported) or `not_found` (absent), never a
  `zlib.error` or index error.
- **No process**: the `src/` ban on `subprocess`/`os.system`/`shell=True`/`eval(`/`exec(` is
  unchanged. Tests create real repositories with the git binary *inside* `tests/`.

`LocalGitClient` enriches a run with the parsed commit and changed paths when the object store is
readable. If an object is missing or corrupt (a shallow or pruned clone), that commit falls back
to the reflog's own words; the reader itself still fails with a coded error when called directly.
The fallback is deliberate: a shallow clone remains usable evidence, and the client does not
pretend it read contents it did not.

## Consequences

- A local run now carries `message`, `parents`, `files`, and the commit's real author/time; the
  normalizer and critics are unchanged.
- Packfile parsing is code we own. It is covered by real `git repack` fixtures (loose, packed,
  delta, truncated pack, unsupported index) and one hand-built `REF_DELTA` pack, because git
  normally emits `OFS_DELTA`.
- Textual diffs, blob contents beyond tree parsing, and SHA-256 repositories are out of scope.
  A SHA-256 repository's objects will fail the SHA-1 id check and degrade to the reflog.
- The dependency surface stays at zero new packages.

## Alternatives considered

- **Shell out to `git cat-file`** — rejected: it reintroduces command execution under `src/`,
  which the allowlist gate and ADR 0008 exist to prevent.
- **Dulwich / GitPython / pygit2** — rejected in the Phase 5 dependency review, and the owner
  chose the standard-library path.
- **Leave the limitation** — rejected by the owner: commit contents and file lists were the one
  remaining gap an operator would hit in daily use.
