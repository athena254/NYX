"""Identity-based deduplication (SPEC §11.2).

Deduplication is keyed on the producer's own identity for the record — a ``producer_event_id``
and/or an ``Idempotency-Key`` supplied on the request — never on content similarity. Two
distinct incidents whose text merely looks alike must not be merged, and this module provides
no path to do so.

A decision is one of:

- ``new`` — no candidate key was seen; the caller may persist and then reserve the keys.
- ``duplicate`` — a key was seen with an identical request hash; the caller returns the
  original record id and does not create a second row.
- ``conflict`` — a key was seen with a different request hash; the caller refuses with
  ``IDEMPOTENCY_KEY_REUSED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from nyx.domain.idempotency import IdempotencyRecord
from nyx.storage.repositories.idempotency import IdempotencyRepository

DedupeStatus = Literal["new", "duplicate", "conflict"]


@dataclass(frozen=True)
class DedupeDecision:
    """The result of comparing a request's dedupe keys against stored identity."""

    status: DedupeStatus
    record_id: str | None = None
    rationale: str = ""


def candidate_keys(*, producer_event_id: str | None, idempotency_key: str | None) -> list[str]:
    """Return the ordered, de-duplicated dedupe keys for a request.

    The explicit idempotency header wins the lookup order but both keys are reserved on
    acceptance so a replay through either identity is recognised.
    """

    ordered = [key for key in (idempotency_key, producer_event_id) if key]
    unique: list[str] = []
    for key in ordered:
        if key not in unique:
            unique.append(key)
    return unique


def evaluate(session: Session, *, scope: str, keys: list[str], request_hash: str) -> DedupeDecision:
    """Classify a request against the stored dedupe keys."""

    repository = IdempotencyRepository(session)
    for key in keys:
        existing = repository.find(scope, key)
        if existing is None:
            continue
        if existing.request_hash == request_hash:
            return DedupeDecision(
                status="duplicate",
                record_id=existing.record_id,
                rationale=(
                    f"{scope} ingestion replay for key {key!r}: identical request hash "
                    f"{request_hash} already produced record {existing.record_id}"
                ),
            )
        return DedupeDecision(
            status="conflict",
            record_id=existing.record_id,
            rationale=(
                f"{scope} idempotency key {key!r} was already used for record "
                f"{existing.record_id} with a different request hash"
            ),
        )
    return DedupeDecision(status="new")


def reserve(
    session: Session,
    *,
    scope: str,
    keys: list[str],
    record_id: str,
    request_hash: str,
    rationale: str,
) -> list[IdempotencyRecord]:
    """Reserve every dedupe key for a newly created record."""

    repository = IdempotencyRepository(session)
    reserved: list[IdempotencyRecord] = []
    for key in keys:
        if repository.find(scope, key) is not None:
            continue
        record = IdempotencyRecord(
            scope=scope,
            key=key,
            record_id=record_id,
            request_hash=request_hash,
            rationale=rationale,
        )
        reserved.append(repository.add(record))
    return reserved
