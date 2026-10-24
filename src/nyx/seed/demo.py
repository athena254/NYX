"""`nyx seed demo`: load the Ghost Factory demonstration (H5).

The seed is a *demonstration*, not a shortcut. It reads the checked-in Ghost Factory fixtures from
``examples/ghost-factory/`` and pushes them through the ordinary
:class:`~nyx.ingestion.service.IngestionService`, so redaction, schema validation, identity
deduplication, and the per-record audit entry apply exactly as they do for producer traffic. A seed
that wrote rows directly would be a second ingestion path, and the whole point of the boundary is
that there is only one.

**It is idempotent by identity.** Every fixture carries a stable ``id`` and ``producer_event_id``,
so the gateway's deduplication turns a second run into a set of duplicates rather than new rows.
Re-running the seed is therefore a no-op, which is what makes it safe to put in a script or a
Makefile.

**The fixtures are the single source.** They used to live under ``tests/fixtures/`` and be copied
into ``examples/ghost-factory/``. One directory now serves both: the tests read the same files the
seed loads, so a fixture that drifts from what the seed ingests cannot exist.

**Nothing here judges.** The seed ingests and stops. It does not run a critic, file a finding, or
record a disposition -- a demonstration that produced its own conclusions would be a second opinion
the operator never asked for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nyx.config import Settings
from nyx.domain.base import utc_now
from nyx.ingestion.errors import IngestResult
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory, create_engine_from_settings, create_session_factory

#: The demonstration's fixtures, in the order they must be loaded. A claim cites an event, and an
#: evidence item is scoped to a claim, so the order is a dependency order rather than a convention.
SEED_ORDER: tuple[str, ...] = (
    "system-manifest.json",
    "event-completion-claim.json",
    "claim-completion-abc123.json",
    "evidence-test-abc123.json",
    "evidence-test-def456.json",
)

#: Where the fixtures live. Relative to the repository root, which is where the CLI runs from.
DEFAULT_FIXTURE_DIR = Path("examples") / "ghost-factory"


class SeedError(Exception):
    """The seed could not be run. Carries a stable ``code`` for the CLI."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SeedOutcome:
    """What one seed run did, in the order it did it."""

    fixtures: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A seed with any rejection is not a success, even if the other fixtures loaded."""

        return not self.rejected

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serialisable representation for the CLI and logs."""

        return {
            "ok": self.ok,
            "fixtures": self.fixtures,
            "created": self.created,
            "duplicates": self.duplicates,
            "rejected": [
                {"fixture": fixture, "reason": reason} for fixture, reason in self.rejected
            ],
        }


def _load_fixture(directory: Path, name: str) -> dict[str, Any]:
    """Read one fixture, refusing anything that is not a JSON object."""

    path = directory / name
    if not path.exists():
        raise SeedError(
            f"seed fixture {path} does not exist",
            code="SEED_FIXTURE_MISSING",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedError(
            f"seed fixture {path} could not be read ({type(exc).__name__})",
            code="SEED_FIXTURE_UNREADABLE",
        ) from exc
    if not isinstance(payload, dict):
        raise SeedError(
            f"seed fixture {path} is not a JSON object",
            code="SEED_FIXTURE_INVALID",
        )
    return payload


def _ingest_one(
    service: IngestionService, payload: dict[str, Any], *, request_id: str
) -> tuple[str, str]:
    """Push one fixture through the ordinary gateway and classify the outcome."""

    if "claim_type" in payload or "statement" in payload:
        result = service.register_claim(payload, request_id=request_id)
    elif payload.get("kind") in {"ci_test_result", "test_result", "observation", "artifact"}:
        result = service.register_evidence(payload, request_id=request_id)
    elif payload.get("event_type") is not None:
        result = service.ingest_event(payload, request_id=request_id)
    elif payload.get("capabilities") is not None or payload.get("owner") is not None:
        result = service.register_system(payload, request_id=request_id)
    else:
        raise SeedError(
            "a seed fixture matches no record type; the seed cannot guess",
            code="SEED_FIXTURE_UNKNOWN_TYPE",
        )
    return _classify(result)


def _classify(result: IngestResult) -> tuple[str, str]:
    """Turn one :class:`IngestResult` into ``(status, record_id)``."""

    status = getattr(result, "status", "rejected")
    if status == "created":
        return "created", str(getattr(result, "record_id", "") or "")
    if status == "duplicate":
        return "duplicate", str(getattr(result, "record_id", "") or "")
    reason = getattr(getattr(result, "error", None), "code", None)
    return "rejected", str(reason or "SCHEMA_VALIDATION_FAILED")


def seed_demo(
    settings: Settings | None = None,
    *,
    session_factory: SessionFactory | None = None,
    fixture_dir: Path | None = None,
) -> SeedOutcome:
    """Load the Ghost Factory demonstration into the store, idempotently.

    Each fixture is ingested in its own transaction, so one bad fixture does not roll back the ones
    before it. A rejection is recorded rather than raised: a caller that asked for a seed wants to
    know which fixtures loaded and which did not, and ``SeedOutcome.ok`` is the single verdict.
    """

    effective = settings or Settings()
    directory = fixture_dir or DEFAULT_FIXTURE_DIR
    if not directory.is_absolute():
        # Resolve against the repository root, not the caller's cwd: `nyx seed demo` is run from
        # the repository root, and a relative path that silently resolved elsewhere would seed a
        # different set of fixtures depending on where the operator stood.
        directory = Path(__file__).resolve().parents[3] / directory

    if not directory.is_dir():
        raise SeedError(
            f"seed fixture directory {directory} does not exist",
            code="SEED_FIXTURE_DIR_MISSING",
        )

    missing = [name for name in SEED_ORDER if not (directory / name).exists()]
    if missing:
        raise SeedError(
            f"seed fixture directory {directory} is missing {', '.join(missing)}",
            code="SEED_FIXTURE_MISSING",
        )

    factory = session_factory or create_session_factory(create_engine_from_settings(effective))
    service = IngestionService(factory, effective)

    outcome = SeedOutcome(fixtures=list(SEED_ORDER))
    for name in SEED_ORDER:
        payload = _load_fixture(directory, name)
        request_id = f"seed-demo-{name}"
        status, identifier = _ingest_one(service, payload, request_id=request_id)
        if status == "created":
            outcome.created.append(name)
        elif status == "duplicate":
            outcome.duplicates.append(name)
        else:
            outcome.rejected.append((name, identifier))

    if session_factory is None:
        # The engine this function created is this function's to dispose.
        factory.kw["bind"].dispose()
    return outcome


def seed_demo_from_settings(settings: Settings | None = None) -> SeedOutcome:
    """Convenience wrapper for the CLI: seed using only the effective settings."""

    return seed_demo(settings, fixture_dir=DEFAULT_FIXTURE_DIR)


__all__ = [
    "DEFAULT_FIXTURE_DIR",
    "SEED_ORDER",
    "SeedError",
    "SeedOutcome",
    "seed_demo",
    "seed_demo_from_settings",
    "utc_now",
]
