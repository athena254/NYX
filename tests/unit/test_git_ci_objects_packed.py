"""Packed-object tests for the local-git object reader (slice H2b).

The first two tests use real repositories and ``git repack``; the last builds a minimal
``REF_DELTA`` pack by hand, because contemporary git prefers ``OFS_DELTA`` and would otherwise
leave the reference-delta branch untested.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from nyx.adapters.git_ci.github_client import GitCiError
from nyx.adapters.git_ci.objects import ObjectReader

GIT = shutil.which("git") or ""
pytestmark = pytest.mark.skipif(not GIT, reason="the git binary is not available")

AUTHOR = "Packed Author"
EMAIL = "packed@example.invalid"


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


def _pack_repository(path: Path, commits: int = 12) -> tuple[Path, list[str]]:
    repo = _repository(path)
    body = "\n".join(f"line {index:04d} of a file that changes slowly" for index in range(400))
    shas: list[str] = []
    for index in range(commits):
        (repo / "data.txt").write_text(f"{body}\nrevision {index}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"Revision {index}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    _git(repo, "repack", "-adf", "--depth=50", "--window=250")
    _git(repo, "prune-packed")
    return repo, shas


def _writable(path: Path) -> None:
    """Git marks packfiles read-only on Windows; these tests corrupt copies on purpose."""

    path.chmod(0o666)


def test_packed_objects_are_read_after_repack(tmp_path: Path) -> None:
    repo, shas = _pack_repository(tmp_path / "repo")
    packs = list((repo / ".git" / "objects" / "pack").glob("*.pack"))
    assert packs, "git repack produced no packfile"
    assert not list((repo / ".git" / "objects").glob("[0-9a-f][0-9a-f]/*")), (
        "loose objects remain, so the packed path would not be exercised"
    )

    verify = _git(repo, "verify-pack", "-v", str(packs[0]))
    delta_lines = [line for line in verify.splitlines() if len(line.split()) >= 7]
    assert delta_lines, "the pack contains no delta objects; the delta path is untested"

    reader = ObjectReader(repo / ".git")
    for sha in shas:
        info = reader.commit(sha)
        assert info.sha == sha
        assert reader.list_files(info.tree) == ["data.txt"]
    assert reader.changed_paths(shas[-1]) == ["data.txt"]
    assert reader.changed_paths(shas[0]) == ["data.txt"]


def test_a_truncated_pack_is_unavailable(tmp_path: Path) -> None:
    repo, shas = _pack_repository(tmp_path / "repo", commits=4)
    pack = next((repo / ".git" / "objects" / "pack").glob("*.pack"))
    broken = tmp_path / "broken"
    shutil.copytree(repo, broken)
    broken_pack = next((broken / ".git" / "objects" / "pack").glob("*.pack"))
    data = broken_pack.read_bytes()
    _writable(broken_pack)
    broken_pack.write_bytes(data[:40])

    with pytest.raises(GitCiError) as excinfo:
        ObjectReader(broken / ".git").read(shas[-1])

    assert excinfo.value.code == "unavailable"
    # The original repository is untouched: this was a copy, not a mutation.
    assert pack.is_file()


def test_an_unsupported_index_version_is_unavailable(tmp_path: Path) -> None:
    repo, shas = _pack_repository(tmp_path / "repo", commits=2)
    idx = next((repo / ".git" / "objects" / "pack").glob("*.idx"))
    raw = bytearray(idx.read_bytes())
    raw[4:8] = (3).to_bytes(4, "big")
    _writable(idx)
    idx.write_bytes(bytes(raw))

    with pytest.raises(GitCiError) as excinfo:
        ObjectReader(repo / ".git").read(shas[-1])

    assert excinfo.value.code == "unavailable"


# --------------------------------------------------------------------------- ref delta


def _header(type_number: int, size: int) -> bytes:
    first = (type_number << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _build_ref_delta_pack(git_dir: Path) -> tuple[str, str, bytes]:
    """Write a two-object pack: a blob, and a ``REF_DELTA`` blob built from it."""

    base = b"hello world\n"
    result = b"hello brave world\n"
    base_sha = hashlib.sha1(b"blob 12\0" + base).hexdigest()  # noqa: S324 - git object id
    result_sha = hashlib.sha1(b"blob 18\0" + result).hexdigest()  # noqa: S324 - git object id

    delta = _varint(len(base)) + _varint(len(result)) + bytes([len(result)]) + result
    base_entry = _header(3, len(base)) + zlib.compress(base)
    delta_entry = _header(7, len(delta)) + bytes.fromhex(base_sha) + zlib.compress(delta)
    body = b"PACK" + struct.pack(">II", 2, 2) + base_entry + delta_entry
    pack_bytes = body + hashlib.sha1(body).digest()  # noqa: S324 - pack trailer is not security

    shas = sorted([base_sha, result_sha])
    fanout = bytearray()
    for prefix in range(256):
        fanout += struct.pack(">I", sum(1 for sha in shas if int(sha[:2], 16) <= prefix))
    offsets = {
        base_sha: 12,
        result_sha: 12 + len(base_entry),
    }
    idx = bytearray(b"\xfftOc" + struct.pack(">I", 2))
    idx += fanout
    idx += b"".join(bytes.fromhex(sha) for sha in shas)
    idx += b"\0" * (4 * len(shas))
    idx += b"".join(struct.pack(">I", offsets[sha]) for sha in shas)
    idx += hashlib.sha1(pack_bytes).digest()  # noqa: S324 - index trailer
    idx += hashlib.sha1(bytes(idx)).digest()  # noqa: S324 - index trailer

    pack_dir = git_dir / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack-synthetic.pack").write_bytes(pack_bytes)
    (pack_dir / "pack-synthetic.idx").write_bytes(bytes(idx))
    return base_sha, result_sha, result


def test_a_reference_delta_is_resolved(tmp_path: Path) -> None:
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)
    base_sha, result_sha, result = _build_ref_delta_pack(git_dir)

    reader = ObjectReader(git_dir)

    assert reader.read(base_sha).data == b"hello world\n"
    obj = reader.read(result_sha)
    assert obj.type == "blob"
    assert obj.data == result
