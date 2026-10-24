"""External audit checkpoints: an out-of-band anchor for the hash chain (H3, ADR 0012).

The chain in ``src/nyx/audit/chain.py`` is a *local* integrity control. SPEC §5.8 asks for a hash
chain **and** that its trust limits are documented, and those limits are real: an actor who can
rewrite the whole database can recompute every entry and the chain still verifies.

A checkpoint is the one thing that closes that gap, at a cost the operator chooses:

* ``nyx audit checkpoint`` appends ``{sequence, head_hash, recorded_at}`` to a JSONL file the
  operator picks. The default lives in ``data_dir``, and the docs say plainly that a checkpoint
  stored next to the database is weaker than one copied elsewhere -- an attacker who rewrites the
  database and recomputes the chain will very likely rewrite a file sitting in the same directory.
* ``nyx audit verify --against FILE`` walks the chain to the checkpointed sequence and compares the
  entry hash. A fully recomputed rewrite therefore **no longer verifies**, because the attacker
  would have had to forge the checkpoint too.

The guarantee is deliberately narrow, and the module says so in three places: this detects a
*rewrite* of already-recorded history. It does not detect an attacker who can forge the checkpoint
file, it does not detect deletion of the checkpoint itself, and it does not make the chain a
signature or a transparency log. A missing or short checkpoint file is an explicit **refusal**, not
a pass -- a checker that silently treats "no checkpoint" as "verified" is worse than no checker.

The file is written by the operator's own process, never by the service, so the module opens it
with plain :mod:`json` and never parses anything it did not write back with its own comparison.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nyx.domain.base import format_utc, utc_now
from nyx.domain.canonical import content_hash

#: The version of the checkpoint record. A file from an older version is refused, not guessed at.
CHECKPOINT_SCHEMA_VERSION = "1.0"

#: What one checkpoint record carries. ``sequence`` and ``head_hash`` are the anchor; the rest is
#: there so an operator can answer "which run of mine was this?" without a second lookup.
CHECKPOINT_FIELDS = ("schema_version", "sequence", "head_hash", "recorded_at", "entries_checked")

#: The reason codes a checkpoint comparison can produce. Stable strings, because a caller may branch
#: on them.
REASON_NO_CHECKPOINT = "no_checkpoint"
REASON_TRUNCATED = "checkpoint_truncated"
REASON_MISMATCH = "checkpoint_hash_mismatch"
REASON_MALFORMED = "malformed_checkpoint"
REASON_SEQUENCE_BEYOND_CHAIN = "checkpoint_sequence_beyond_chain"


class CheckpointError(Exception):
    """A checkpoint could not be written or read. Carries a stable ``code`` for the CLI."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _is_digest(value: str) -> bool:
    """Whether ``value`` is one of NYX's own digests: ``sha256:`` plus 64 hex characters."""

    return (
        value.startswith("sha256:")
        and len(value) == len("sha256:") + 64
        and all(character in "0123456789abcdef" for character in value[len("sha256:") :])
    )


@dataclass(frozen=True)
class CheckpointRecord:
    """One line of a checkpoint file. Frozen: a checkpoint is a record, not a working object."""

    schema_version: str
    sequence: int
    head_hash: str
    recorded_at: str
    entries_checked: int

    def as_dict(self) -> dict[str, Any]:
        """The JSON object written to the file. Field order is fixed so the file diffs cleanly."""

        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "head_hash": self.head_hash,
            "recorded_at": self.recorded_at,
            "entries_checked": self.entries_checked,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CheckpointRecord:
        """Parse one record, refusing anything the writer would not have produced.

        Raises :class:`CheckpointError` with ``MALFORMED_CHECKPOINT`` rather than returning a
        partial record: a checkpoint that is half-readable is a checkpoint an operator cannot trust,
        and the caller needs a stable code to report.
        """

        missing = [field for field in CHECKPOINT_FIELDS if field not in payload]
        if missing:
            raise CheckpointError(
                f"checkpoint record is missing {', '.join(missing)}",
                code=REASON_MALFORMED,
            )
        if payload["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointError(
                f"checkpoint schema_version {payload['schema_version']!r} is not supported "
                f"(expected {CHECKPOINT_SCHEMA_VERSION!r})",
                code=REASON_MALFORMED,
            )
        sequence = payload["sequence"]
        entries_checked = payload["entries_checked"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise CheckpointError(
                f"checkpoint sequence {sequence!r} is not a positive integer",
                code=REASON_MALFORMED,
            )
        if (
            not isinstance(entries_checked, int)
            or isinstance(entries_checked, bool)
            or entries_checked < sequence
        ):
            raise CheckpointError(
                f"checkpoint entries_checked {entries_checked!r} is not a count at or after "
                "the checkpointed sequence",
                code=REASON_MALFORMED,
            )
        head_hash = payload["head_hash"]
        if not isinstance(head_hash, str) or not _is_digest(head_hash):
            raise CheckpointError(
                f"checkpoint head_hash {head_hash!r} is not a sha256 digest",
                code=REASON_MALFORMED,
            )
        recorded_at = payload["recorded_at"]
        if not isinstance(recorded_at, str) or not recorded_at:
            raise CheckpointError(
                "checkpoint recorded_at is not a non-empty string",
                code=REASON_MALFORMED,
            )
        return cls(
            schema_version=payload["schema_version"],
            sequence=sequence,
            head_hash=head_hash,
            recorded_at=recorded_at,
            entries_checked=entries_checked,
        )


@dataclass(frozen=True)
class CheckpointComparison:
    """The outcome of comparing the stored chain against one checkpoint file."""

    ok: bool
    reason: str
    checkpoint_sequence: int | None = None
    checkpoint_head_hash: str | None = None
    stored_head_hash: str | None = None
    entries_checked: int = 0

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serialisable representation for the CLI and logs."""

        return {
            "ok": self.ok,
            "reason": self.reason,
            "checkpoint_sequence": self.checkpoint_sequence,
            "checkpoint_head_hash": self.checkpoint_head_hash,
            "stored_head_hash": self.stored_head_hash,
            "entries_checked": self.entries_checked,
        }


@dataclass(frozen=True)
class _ChainHead:
    """The top of the stored chain, read once."""

    sequence: int
    head_hash: str | None
    entries_checked: int


def _read_head(session_factory: Any, *, page_size: int = 500) -> _ChainHead:
    """Walk the stored chain far enough to know its top, without materialising it all."""

    from nyx.storage.db import session_scope
    from nyx.storage.repositories.audit import AuditRepository

    with session_scope(session_factory) as session:
        repository = AuditRepository(session)
        offset = 0
        last: Any = None
        while True:
            page = repository.list(limit=page_size, offset=offset)
            if not page:
                break
            last = page[-1]
            if len(page) < page_size:
                break
            offset += page_size
    if last is None:
        return _ChainHead(sequence=0, head_hash=None, entries_checked=0)
    return _ChainHead(
        sequence=last.sequence, head_hash=last.entry_hash, entries_checked=last.sequence
    )


def write_checkpoint(
    session_factory: Any,
    *,
    path: Path,
    clock: Callable[[], datetime] = utc_now,
    logger: logging.Logger | None = None,
) -> CheckpointRecord:
    """Append one checkpoint record for the current chain head and return it.

    The parent directory is created when missing, because a checkpoint command run by an operator
    should not require the operator to have pre-created a directory. The append is a single
    ``write`` of one line under ``O_APPEND`` semantics via :meth:`Path.open` in ``a`` mode, so two
    checkpoints racing cannot interleave half a line.

    Raises :class:`CheckpointError` with ``CHECKPOINT_UNWRITABLE`` when the file cannot be written.
    A checkpoint that was not written must be an error, not a no-op that looks like success.
    """

    head = _read_head(session_factory)
    if head.head_hash is None:
        # An empty chain has nothing to anchor. Recording a checkpoint for sequence 0 would be a
        # checkpoint of nothing, and comparing it later would pass vacuously.
        raise CheckpointError(
            "the audit chain is empty, so there is no head to checkpoint",
            code=REASON_NO_CHECKPOINT,
        )

    record = CheckpointRecord(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        sequence=head.sequence,
        head_hash=head.head_hash,
        recorded_at=format_utc(clock()),
        entries_checked=head.entries_checked,
    )
    line = json.dumps(record.as_dict(), sort_keys=True, separators=(",", ":"))

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{line}\n")
    except OSError as exc:
        raise CheckpointError(
            f"could not write the checkpoint file {path} ({type(exc).__name__})",
            code="CHECKPOINT_UNWRITABLE",
        ) from exc

    log = logger or logging.getLogger("nyx.audit")
    log.info(
        "audit checkpoint written: sequence=%s entries=%s file=%s",
        record.sequence,
        record.entries_checked,
        path,
    )
    return record


def _read_checkpoint_file(path: Path) -> list[CheckpointRecord]:
    """Read every checkpoint record in a file, oldest first.

    Raises :class:`CheckpointError` with ``CHECKPOINT_UNREADABLE`` when the file cannot be read,
    and ``MALFORMED_CHECKPOINT`` for any line that is not a valid record. An empty file is a
    malformed checkpoint, not a checkpoint of nothing: the caller asked to verify *against*
    something and there is nothing there.
    """

    if not path.exists():
        raise CheckpointError(
            f"checkpoint file {path} does not exist",
            code=REASON_NO_CHECKPOINT,
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckpointError(
            f"could not read the checkpoint file {path} ({type(exc).__name__})",
            code="CHECKPOINT_UNREADABLE",
        ) from exc

    records: list[CheckpointRecord] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CheckpointError(
                f"checkpoint line {number} in {path} is not valid JSON",
                code=REASON_MALFORMED,
            ) from exc
        if not isinstance(payload, dict):
            raise CheckpointError(
                f"checkpoint line {number} in {path} is not a JSON object",
                code=REASON_MALFORMED,
            )
        try:
            records.append(CheckpointRecord.from_dict(payload))
        except CheckpointError as exc:
            raise CheckpointError(
                f"checkpoint line {number} in {path}: {exc}",
                code=exc.code,
            ) from exc

    if not records:
        raise CheckpointError(
            f"checkpoint file {path} holds no checkpoint records",
            code=REASON_NO_CHECKPOINT,
        )
    return records


def compare_against_checkpoint(session_factory: Any, *, path: Path) -> CheckpointComparison:
    """Compare the stored chain against the latest checkpoint record in ``path``.

    The comparison is a walk, not a full materialisation: the chain is verified to the checkpointed
    sequence and the head hash at that sequence is compared. ``AuditVerifier`` already reports the
    first divergence within the stored sequence; this function answers a different question --
    *is the stored history the history I anchored?*

    Returns a :class:`CheckpointComparison` whose ``reason`` is always set. ``ok`` is true only for
    ``checkpoint_verified``. A refusal (``checkpoint_truncated``, ``checkpoint_hash_mismatch``,
    ``checkpoint_sequence_beyond_chain``, ``no_checkpoint``, ``malformed_checkpoint``) is **not**
    ok: an operator who asked for this check gets a red result, never a silent pass.

    Raises :class:`CheckpointError` for a checkpoint file that cannot be read at all or that is
    malformed. Those are operator errors in the *input to the check*, not verdicts about the chain,
    and the CLI reports them as a failure with a stable code rather than as a comparison result.
    """

    try:
        records = _read_checkpoint_file(path)
    except CheckpointError as exc:
        if exc.code == REASON_NO_CHECKPOINT:
            # An absent or empty file is a *refusal*, not an input error: the operator asked to
            # verify against a checkpoint and there is none. A red result, not an exception.
            head = _read_head(session_factory)
            return CheckpointComparison(
                ok=False,
                reason=REASON_NO_CHECKPOINT,
                checkpoint_sequence=None,
                checkpoint_head_hash=None,
                stored_head_hash=head.head_hash,
                entries_checked=head.entries_checked,
            )
        raise

    latest = records[-1]
    head = _read_head(session_factory)

    if head.head_hash is None:
        return CheckpointComparison(
            ok=False,
            reason=REASON_NO_CHECKPOINT,
            checkpoint_sequence=latest.sequence,
            checkpoint_head_hash=latest.head_hash,
            stored_head_hash=None,
            entries_checked=0,
        )

    if latest.sequence > head.sequence:
        return CheckpointComparison(
            ok=False,
            reason=REASON_SEQUENCE_BEYOND_CHAIN,
            checkpoint_sequence=latest.sequence,
            checkpoint_head_hash=latest.head_hash,
            stored_head_hash=head.head_hash,
            entries_checked=head.entries_checked,
        )

    stored_at_sequence = _hash_at_sequence(session_factory, latest.sequence)
    if stored_at_sequence is None:
        # The chain was truncated below the checkpointed sequence: entries were deleted, not
        # rewritten. That is a divergence the plain verifier reports as a sequence gap, and it is a
        # failed checkpoint comparison for the same reason.
        return CheckpointComparison(
            ok=False,
            reason=REASON_TRUNCATED,
            checkpoint_sequence=latest.sequence,
            checkpoint_head_hash=latest.head_hash,
            stored_head_hash=None,
            entries_checked=head.entries_checked,
        )

    if stored_at_sequence != latest.head_hash:
        return CheckpointComparison(
            ok=False,
            reason=REASON_MISMATCH,
            checkpoint_sequence=latest.sequence,
            checkpoint_head_hash=latest.head_hash,
            stored_head_hash=stored_at_sequence,
            entries_checked=head.entries_checked,
        )

    return CheckpointComparison(
        ok=True,
        reason="checkpoint_verified",
        checkpoint_sequence=latest.sequence,
        checkpoint_head_hash=latest.head_hash,
        stored_head_hash=stored_at_sequence,
        entries_checked=head.entries_checked,
    )


def _hash_at_sequence(session_factory: Any, sequence: int, *, page_size: int = 500) -> str | None:
    """Return ``entry_hash`` for ``sequence``, or ``None`` when the chain does not reach it.

    The chain is append-only and ordered by ``sequence``, so ``sequence`` is on the page that
    starts at ``(sequence - 1) // page_size * page_size`` and the walk starts there. A sequence the
    chain has already passed is still on that page (the page holds ``sequence``..``sequence +
    page_size - 1`` only when the chain is at least that long), so the comparison below is the
    authority: an out-of-range sequence falls off the end of the last page and returns ``None``.
    """

    from nyx.storage.db import session_scope
    from nyx.storage.repositories.audit import AuditRepository

    with session_scope(session_factory) as session:
        repository = AuditRepository(session)
        offset = (sequence - 1) // page_size * page_size
        while True:
            page = repository.list(limit=page_size, offset=offset)
            if not page:
                return None
            for entry in page:
                if entry.sequence == sequence:
                    return entry.entry_hash
            if len(page) < page_size:
                return None
            offset += page_size


def checkpoint_digest(records: list[CheckpointRecord]) -> str:
    """The digest of a whole checkpoint file, for logging which file was verified.

    A digest of the *records as read*, not of the raw text: two files with the same records in the
    same order are the same anchor, and a whitespace difference is not a security difference.
    """

    payload = [record.as_dict() for record in records]
    return content_hash({"checkpoints": payload})


__all__ = [
    "CHECKPOINT_FIELDS",
    "CHECKPOINT_SCHEMA_VERSION",
    "CheckpointComparison",
    "CheckpointError",
    "CheckpointRecord",
    "REASON_MALFORMED",
    "REASON_MISMATCH",
    "REASON_NO_CHECKPOINT",
    "REASON_SEQUENCE_BEYOND_CHAIN",
    "REASON_TRUNCATED",
    "checkpoint_digest",
    "compare_against_checkpoint",
    "write_checkpoint",
]
