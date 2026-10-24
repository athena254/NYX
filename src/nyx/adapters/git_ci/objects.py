"""Read git objects from a local repository (phase 5 follow-up, slices H2a/H2b).

``LocalGitClient`` originally read refs and the reflog only: a commit's sha, author, time and
subject, but not its contents. This module lifts that limitation with the standard library
alone — the dependency review that rejected dulwich/GitPython/pygit2 still holds, and no process
is started.

**Scope.** Loose objects (``objects/<2-hex>/<38-hex>``, zlib-deflated) and packfiles
(``objects/pack/*.idx`` v2 + ``*.pack``), including ``OFS_DELTA`` and ``REF_DELTA``. Commit
metadata (tree, parents, author/committer, message), tree listings, and a *changed-path set*
against the first parent are read. **Textual diffs are not produced**: a path change set is the
maximum claim, and :mod:`nyx.adapters.git_ci.local_git` says so.

**Failures.** Every malformed input is a :class:`~nyx.adapters.git_ci.github_client.GitCiError`
with a stable code: an object the repository does not contain is ``not_found``; anything that
exists but cannot be decoded (a corrupt loose file, a truncated pack, an unsupported index
version, a delta cycle) is ``unavailable``. No ``zlib.error``, ``IndexError`` or ``KeyError``
escapes.

**Bounds.** Objects are capped at ``max_object_bytes`` (default 64 MiB), delta chains at
``max_delta_depth``, and file listings at ``max_files``. The caps are enforced while decoding,
not after allocating.

Only the standard library and the adapter's own sibling ``github_client`` (for the error type and
codes) are imported, so the adapter boundary (ADR 0008) is preserved.
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

from nyx.adapters.git_ci.github_client import CODE_NOT_FOUND, CODE_UNAVAILABLE, GitCiError

#: Git object type numbers, as they appear in a loose header and a pack object header.
_TYPES: Final[dict[int, str]] = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
#: Tree entry modes that name another tree and can be recursed into.
_TREE_MODE: Final = "40000"
#: A gitlink (submodule commit) is not part of this repository's object set.
_GITLINK_MODE: Final = "160000"
#: The pack index magic and the only version this reader accepts.
_IDX_MAGIC: Final = b"\xfftOc"
_IDX_VERSION: Final = 2
#: Pack object header types for the two delta encodings.
_OFS_DELTA: Final = 6
_REF_DELTA: Final = 7
#: The default object cap: large enough for real commits, small enough to bound a bad file.
_DEFAULT_MAX_OBJECT_BYTES: Final = 64 * 1024 * 1024
_DEFAULT_MAX_FILES: Final = 1000
_DEFAULT_MAX_DELTA_DEPTH: Final = 64


@dataclass(frozen=True)
class GitObject:
    """One decoded object: its type name and its payload bytes."""

    type: str
    data: bytes


@dataclass(frozen=True)
class Commit:
    """The commit metadata this reader can defend: tree, parents, people, times, message."""

    sha: str
    tree: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    authored_at: datetime
    committer_name: str
    committer_email: str
    committed_at: datetime
    message: str


@dataclass(frozen=True)
class TreeEntry:
    """One tree entry: git's mode string, the name, and the object it points at."""

    mode: str
    name: str
    sha: str

    @property
    def is_tree(self) -> bool:
        return self.mode == _TREE_MODE

    @property
    def is_gitlink(self) -> bool:
        return self.mode == _GITLINK_MODE


def _unavailable(detail: str, cause: BaseException | None = None) -> GitCiError:
    """Build the stable "present but unreadable" error, keeping the cause for logs only."""

    error = GitCiError(detail, code=CODE_UNAVAILABLE)
    if cause is not None:
        error.__cause__ = cause
    return error


def _not_found(detail: str) -> GitCiError:
    return GitCiError(detail, code=CODE_NOT_FOUND)


def _offset(tz: str) -> timezone:
    """Turn git's ``+0300`` into a ``timezone``; a naive timestamp is never produced."""

    sign = -1 if tz.startswith("-") else 1
    return timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])))


def _parse_ident(value: str, field: str) -> tuple[str, str, datetime]:
    """Parse ``Name <email> <unix-ts> <tz>`` into its three typed parts."""

    name, sep, rest = value.partition(" <")
    if not sep:
        raise _unavailable(f"commit {field} line has no email bracket")
    email, sep, stamps = rest.partition("> ")
    if not sep:
        raise _unavailable(f"commit {field} line has no timestamp")
    ts, _, tz = stamps.partition(" ")
    if not ts.isdigit() or len(tz) != 5:
        raise _unavailable(f"commit {field} timestamp is malformed")
    try:
        moment = datetime.fromtimestamp(int(ts), tz=_offset(tz))
    except (ValueError, OverflowError, OSError) as exc:
        raise _unavailable(f"commit {field} timestamp is out of range", exc) from exc
    return name, email, moment


def _varint(data: bytes, position: int) -> tuple[int, int]:
    """Read a little-endian 7-bit varint (pack object size, delta sizes)."""

    value = 0
    shift = 0
    while True:
        if position >= len(data):
            raise _unavailable("varint runs past the end of the object")
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
        if shift > 63:
            raise _unavailable("varint is unreasonably long")


def _inflate(data: bytes, expected: int, *, limit: int) -> tuple[bytes, int]:
    """Inflate exactly ``expected`` bytes and return them plus the compressed length."""

    if expected > limit:
        raise _unavailable(f"object declares {expected} bytes, above the configured cap")
    inflater = zlib.decompressobj()
    out = bytearray()
    feed = data
    consumed = 0
    try:
        while True:
            chunk = inflater.decompress(feed, expected + 1 - len(out))
            out.extend(chunk)
            consumed += len(feed) - len(inflater.unconsumed_tail)
            if len(out) > expected:
                raise _unavailable("object inflates beyond its declared size")
            if inflater.eof:
                consumed -= len(inflater.unused_data)
                break
            feed = inflater.unconsumed_tail
            if not feed:
                raise _unavailable("object stream ended before it was complete")
    except zlib.error as exc:
        raise _unavailable("object stream is not valid zlib data", exc) from exc
    if len(out) != expected:
        raise _unavailable("object inflates to a different size than declared")
    return bytes(out), consumed


def _apply_delta(base: bytes, delta: bytes, *, limit: int) -> bytes:
    """Apply a git delta instruction stream to ``base``."""

    base_size, position = _varint(delta, 0)
    result_size, position = _varint(delta, position)
    if base_size != len(base):
        raise _unavailable("delta base size does not match the base object")
    if result_size > limit:
        raise _unavailable(f"delta result declares {result_size} bytes, above the cap")

    out = bytearray()
    while position < len(delta):
        opcode = delta[position]
        position += 1
        if opcode & 0x80:
            copy_offset = 0
            copy_size = 0
            for bit, shift in ((0x01, 0), (0x02, 8), (0x04, 16), (0x08, 24)):
                if opcode & bit:
                    if position >= len(delta):
                        raise _unavailable("delta copy offset runs past the stream")
                    copy_offset |= delta[position] << shift
                    position += 1
            for bit, shift in ((0x10, 0), (0x20, 8), (0x40, 16)):
                if opcode & bit:
                    if position >= len(delta):
                        raise _unavailable("delta copy size runs past the stream")
                    copy_size |= delta[position] << shift
                    position += 1
            if copy_size == 0:
                copy_size = 0x10000
            if copy_offset + copy_size > len(base):
                raise _unavailable("delta copy is outside the base object")
            out.extend(base[copy_offset : copy_offset + copy_size])
        elif opcode:
            end = position + opcode
            if end > len(delta):
                raise _unavailable("delta insert runs past the stream")
            out.extend(delta[position:end])
            position = end
        else:
            raise _unavailable("delta stream contains the reserved zero opcode")
        if len(out) > result_size:
            raise _unavailable("delta produces more bytes than it declares")
    if len(out) != result_size:
        raise _unavailable("delta produces fewer bytes than it declares")
    return bytes(out)


class _Pack:
    """One ``.idx``/``.pack`` pair, decoded lazily and cached per offset."""

    def __init__(self, idx_path: Path, pack_path: Path, reader: ObjectReader) -> None:
        self._reader = reader
        self._idx_path = idx_path
        self._pack_path = pack_path
        self._offsets: dict[str, int] = {}
        self._pack: bytes | None = None
        self._cache: dict[int, GitObject] = {}
        self._load_index()

    def _load_index(self) -> None:
        try:
            raw = self._idx_path.read_bytes()
        except OSError as exc:
            raise _unavailable("could not read a pack index", exc) from exc
        if raw[:4] != _IDX_MAGIC:
            raise _unavailable("pack index is not a version 2 index")
        version = int.from_bytes(raw[4:8], "big")
        if version != _IDX_VERSION:
            raise _unavailable(f"pack index version {version} is not supported")

        fanout = raw[8 : 8 + 256 * 4]
        if len(fanout) != 256 * 4:
            raise _unavailable("pack index fanout table is truncated")
        count = int.from_bytes(fanout[255 * 4 : 256 * 4], "big")
        sha_start = 8 + 256 * 4
        sha_end = sha_start + count * 20
        # v2 layout: shas, then one CRC per object, then one offset per object.
        offset_start = sha_end + count * 4
        offset_end = offset_start + count * 4
        large_start = offset_end
        if len(raw) < large_start:
            raise _unavailable("pack index is truncated")

        shas = raw[sha_start:sha_end]
        for index in range(count):
            sha = shas[index * 20 : (index + 1) * 20].hex()
            value = int.from_bytes(
                raw[offset_start + index * 4 : offset_start + index * 4 + 4], "big"
            )
            if value & 0x80000000:
                large_index = value & 0x7FFFFFFF
                start = large_start + large_index * 8
                if start + 8 > len(raw):
                    raise _unavailable("pack index large-offset table is truncated")
                value = int.from_bytes(raw[start : start + 8], "big")
            self._offsets[sha] = value

    def contains(self, sha: str) -> bool:
        return sha in self._offsets

    def _pack_bytes(self) -> bytes:
        if self._pack is not None:
            return self._pack
        try:
            size = self._pack_path.stat().st_size
        except OSError as exc:
            raise _unavailable("could not stat a packfile", exc) from exc
        if size > self._reader.max_pack_bytes:
            raise _unavailable("packfile is larger than the configured cap")
        try:
            raw = self._pack_path.read_bytes()
        except OSError as exc:
            raise _unavailable("could not read a packfile", exc) from exc
        if raw[:4] != b"PACK":
            raise _unavailable("packfile has no PACK header")
        version = int.from_bytes(raw[4:8], "big")
        if version not in (2, 3):
            raise _unavailable(f"packfile version {version} is not supported")
        self._pack = raw
        return raw

    def read(self, sha: str, *, depth: int = 0) -> GitObject:
        try:
            offset = self._offsets[sha]
        except KeyError as exc:
            raise _not_found(f"object {sha} is not in this pack") from exc
        return self.read_at(offset, depth=depth)

    def read_at(self, offset: int, *, depth: int = 0) -> GitObject:
        cached = self._cache.get(offset)
        if cached is not None:
            return cached
        if depth > self._reader.max_delta_depth:
            raise _unavailable("delta chain is deeper than the configured limit")

        raw = self._pack_bytes()
        if offset < 12 or offset >= len(raw):
            raise _unavailable("pack object offset is outside the packfile")
        position = offset
        header = raw[position]
        position += 1
        type_number = (header >> 4) & 0x07
        size = header & 0x0F
        shift = 4
        while header & 0x80:
            if position >= len(raw):
                raise _unavailable("pack object header is truncated")
            header = raw[position]
            position += 1
            size |= (header & 0x7F) << shift
            shift += 7
            if shift > 63:
                raise _unavailable("pack object header is unreasonably long")

        if type_number in _TYPES:
            payload, _ = _inflate(
                raw[position:],
                size,
                limit=min(self._reader.max_object_bytes, len(raw)),
            )
            result = GitObject(type=_TYPES[type_number], data=payload)
        elif type_number == _OFS_DELTA:
            # The base offset precedes the compressed delta, so it is consumed first.
            base_offset, position = self._ofs_base(offset, position, raw)
            payload, _ = _inflate(
                raw[position:],
                size,
                limit=min(self._reader.max_object_bytes, len(raw)),
            )
            base = self.read_at(base_offset, depth=depth + 1)
            result = GitObject(
                type=base.type,
                data=_apply_delta(base.data, payload, limit=self._reader.max_object_bytes),
            )
        elif type_number == _REF_DELTA:
            # Likewise, the 20-byte base sha is plain bytes before the compressed delta.
            if position + 20 > len(raw):
                raise _unavailable("reference delta has no base sha")
            base_sha = raw[position : position + 20].hex()
            position += 20
            payload, _ = _inflate(
                raw[position:],
                size,
                limit=min(self._reader.max_object_bytes, len(raw)),
            )
            base = self._reader.read(base_sha, depth=depth + 1)
            result = GitObject(
                type=base.type,
                data=_apply_delta(base.data, payload, limit=self._reader.max_object_bytes),
            )
        else:
            raise _unavailable(f"pack object type {type_number} is not supported")

        self._cache[offset] = result
        return result

    @staticmethod
    def _ofs_base(offset: int, position: int, raw: bytes) -> tuple[int, int]:
        """Decode an ``OFS_DELTA`` negative base offset, returning ``(base, next_position)``."""

        first = raw[position]
        position += 1
        value = first & 0x7F
        while first & 0x80:
            if position >= len(raw):
                raise _unavailable("delta base offset is truncated")
            first = raw[position]
            position += 1
            value = ((value + 1) << 7) | (first & 0x7F)
        base = offset - value
        if base < 0:
            raise _unavailable("delta base offset points before the packfile")
        return base, position


class ObjectReader:
    """Read loose and packed objects from one ``.git`` directory.

    The reader holds no lock and caches only what it has decoded, so a repository that changes
    between reads is observed again rather than served stale. Packfiles are discovered per
    read; a pack added after construction is still found.
    """

    def __init__(
        self,
        git_dir: Path,
        *,
        max_object_bytes: int = _DEFAULT_MAX_OBJECT_BYTES,
        max_pack_bytes: int = 512 * 1024 * 1024,
        max_files: int = _DEFAULT_MAX_FILES,
        max_delta_depth: int = _DEFAULT_MAX_DELTA_DEPTH,
    ) -> None:
        self._git_dir = Path(git_dir)
        self._objects = self._git_dir / "objects"
        self.max_object_bytes = max_object_bytes
        self.max_pack_bytes = max_pack_bytes
        self.max_files = max_files
        self.max_delta_depth = max_delta_depth
        self._loose_cache: dict[str, GitObject] = {}
        self._packs: list[_Pack] | None = None

    # -- object lookup -----------------------------------------------------

    def read(self, sha: str, *, depth: int = 0) -> GitObject:
        """Return one object by full sha, or raise a coded ``GitCiError``."""

        sha = sha.strip().lower()
        if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
            raise _not_found(f"{sha!r} is not a full git object id")

        cached = self._loose_cache.get(sha)
        if cached is not None:
            return cached

        loose = self._objects / sha[:2] / sha[2:]
        if loose.is_file():
            obj = self._read_loose(loose, sha)
            self._loose_cache[sha] = obj
            return obj

        for pack in self._load_packs():
            if pack.contains(sha):
                obj = pack.read(sha, depth=depth)
                # The index is a claim; the content is the evidence. Verify it matches the
                # name it was found under, so a corrupt pack cannot return another object.
                self._verify(sha, obj)
                self._loose_cache[sha] = obj
                return obj

        raise _not_found(f"object {sha} is not present in this repository")

    def _read_loose(self, path: Path, expected_sha: str) -> GitObject:
        try:
            compressed = path.read_bytes()
        except OSError as exc:
            raise _unavailable("could not read a loose object", exc) from exc
        if len(compressed) > self.max_pack_bytes:
            raise _unavailable("loose object file is larger than the configured cap")
        try:
            raw = zlib.decompress(compressed)
        except zlib.error as exc:
            raise _unavailable("loose object is not valid zlib data", exc) from exc
        header, separator, payload = raw.partition(b"\0")
        if not separator:
            raise _unavailable("loose object has no header terminator")
        kind_raw, _, size_raw = header.partition(b" ")
        try:
            kind = kind_raw.decode("ascii")
            size = int(size_raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise _unavailable("loose object header is malformed", exc) from exc
        if kind not in _TYPES.values():
            raise _unavailable(f"loose object type {kind!r} is not supported")
        if size != len(payload):
            raise _unavailable("loose object payload does not match its declared size")
        if size > self.max_object_bytes:
            raise _unavailable("loose object is larger than the configured cap")
        obj = GitObject(type=kind, data=payload)
        self._verify(expected_sha, obj)
        return obj

    def _verify(self, expected_sha: str, obj: GitObject) -> None:
        # SHA-1 is git's object-id function, not a security primitive; the check detects
        # corruption, and the threat model does not claim collision resistance.
        digest = hashlib.sha1(  # noqa: S324
            obj.type.encode("ascii") + b" " + str(len(obj.data)).encode("ascii") + b"\0" + obj.data
        ).hexdigest()
        if digest != expected_sha:
            raise _unavailable("object content does not hash to the id it was read by")

    def _load_packs(self) -> list[_Pack]:
        if self._packs is not None:
            return self._packs
        packs: list[_Pack] = []
        pack_dir = self._objects / "pack"
        if pack_dir.is_dir():
            for idx in sorted(pack_dir.glob("*.idx")):
                pack_path = idx.with_suffix(".pack")
                if not pack_path.is_file():
                    continue
                packs.append(_Pack(idx, pack_path, self))
        self._packs = packs
        return packs

    # -- commit / tree parsing --------------------------------------------

    def commit(self, sha: str) -> Commit:
        """Parse one commit, refusing any object that is not a commit."""

        obj = self.read(sha)
        if obj.type != "commit":
            raise _unavailable(f"object {sha} is a {obj.type}, not a commit")
        text = obj.data.decode("utf-8", errors="replace")
        header, _, message = text.partition("\n\n")
        tree: str | None = None
        parents: list[str] = []
        author: tuple[str, str, datetime] | None = None
        committer: tuple[str, str, datetime] | None = None
        for line in header.splitlines():
            if line.startswith(" "):
                # Continuation of a multi-line header such as `gpgsig`; not read here.
                continue
            key, _, value = line.partition(" ")
            if key == "tree":
                tree = value.strip()
            elif key == "parent":
                parents.append(value.strip())
            elif key == "author":
                author = _parse_ident(value, "author")
            elif key == "committer":
                committer = _parse_ident(value, "committer")
        if tree is None or author is None or committer is None:
            raise _unavailable(f"commit {sha} is missing a required header")
        return Commit(
            sha=sha,
            tree=tree,
            parents=tuple(parents),
            author_name=author[0],
            author_email=author[1],
            authored_at=author[2],
            committer_name=committer[0],
            committer_email=committer[1],
            committed_at=committer[2],
            message=message,
        )

    def tree(self, sha: str) -> list[TreeEntry]:
        """Parse a tree object into its entries, in git's own order (it sorts them)."""

        obj = self.read(sha)
        if obj.type != "tree":
            raise _unavailable(f"object {sha} is a {obj.type}, not a tree")
        entries: list[TreeEntry] = []
        position = 0
        raw = obj.data
        while position < len(raw):
            space = raw.find(b" ", position)
            nul = raw.find(b"\0", space + 1)
            if space < 0 or nul < 0 or nul + 21 > len(raw):
                raise _unavailable("tree entry is truncated")
            mode = raw[position:space].decode("ascii", errors="replace")
            name = raw[space + 1 : nul].decode("utf-8", errors="replace")
            sha_bytes = raw[nul + 1 : nul + 21]
            entries.append(TreeEntry(mode=mode, name=name, sha=sha_bytes.hex()))
            position = nul + 21
        return entries

    def list_files(self, tree_sha: str) -> list[str]:
        """Every non-gitlink path under ``tree_sha``, capped at ``max_files``."""

        files: list[str] = []
        self._walk_tree(tree_sha, prefix="", files=files)
        return files

    def _walk_tree(self, tree_sha: str, *, prefix: str, files: list[str]) -> None:
        for entry in self.tree(tree_sha):
            if len(files) >= self.max_files:
                return
            path = f"{prefix}{entry.name}"
            if entry.is_gitlink:
                continue
            if entry.is_tree:
                self._walk_tree(entry.sha, prefix=f"{path}/", files=files)
            else:
                files.append(path)

    def changed_paths(self, sha: str) -> list[str]:
        """Paths changed by ``sha`` against its first parent (all files for a root commit)."""

        commit = self.commit(sha)
        if not commit.parents:
            return self.list_files(commit.tree)
        parent = self.commit(commit.parents[0])
        return self._diff_trees(parent.tree, commit.tree, prefix="")

    def _diff_trees(self, old_sha: str, new_sha: str, *, prefix: str) -> list[str]:
        if old_sha == new_sha:
            return []
        old = {entry.name: entry for entry in self.tree(old_sha)}
        new = {entry.name: entry for entry in self.tree(new_sha)}
        changed: list[str] = []
        for name in sorted(set(old) | set(new)):
            before = old.get(name)
            after = new.get(name)
            path = f"{prefix}{name}"
            if len(changed) >= self.max_files:
                break
            if before is not None and after is not None:
                if before.is_tree and after.is_tree:
                    changed.extend(self._diff_trees(before.sha, after.sha, prefix=f"{path}/"))
                elif before.is_gitlink or after.is_gitlink:
                    if before.sha != after.sha or before.mode != after.mode:
                        changed.append(path)
                elif before.sha != after.sha or before.mode != after.mode:
                    changed.append(path)
            elif before is not None:
                if before.is_tree:
                    changed.extend(self._files_under(before.sha, prefix=f"{path}/"))
                elif not before.is_gitlink:
                    changed.append(path)
            elif after is not None and not after.is_gitlink:
                if after.is_tree:
                    changed.extend(self._files_under(after.sha, prefix=f"{path}/"))
                else:
                    changed.append(path)
        return changed

    def _files_under(self, tree_sha: str, *, prefix: str) -> list[str]:
        files: list[str] = []
        self._walk_tree(tree_sha, prefix=prefix, files=files)
        return files[: max(0, self.max_files)]


__all__ = ["Commit", "GitObject", "ObjectReader", "TreeEntry"]
