"""Typed errors for the agent-transcript adapters (mirrors the git-ci adapter's error shape).

Two refusals, deliberately distinguished, exactly as the git-ci adapter distinguishes them:

* :class:`TranscriptError` is a *source* failure — the transcript store is missing, unreadable,
  or otherwise cannot be observed. It **propagates** out of ``fetch_for_ref``: "the adapter is
  down" is an error a caller must retry, not an empty observation. An empty list means "the
  source is readable and held no records"; a missing database is not that.
* :class:`NormalizationError` names the offending field of a malformed record. A single bad row
  must not hide the good ones, so the reader raises it per-record and the collector decides
  whether to skip or propagate, exactly as the git-ci normalizer does.
"""

from __future__ import annotations


class TranscriptError(Exception):
    """The transcript source could not be observed (missing, unreadable, or locked)."""

    code: str = "transcript_unavailable"

    def __init__(self, message: str, *, code: str = "transcript_unavailable") -> None:
        super().__init__(message)
        self.code = code


class NormalizationError(ValueError):
    """A single transcript record was malformed; names the offending field."""

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


__all__ = ["NormalizationError", "TranscriptError"]
