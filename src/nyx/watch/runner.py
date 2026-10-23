"""The watcher's cycle: one pass over the enabled adapters (ADR 0009).

A cycle is **read-only**. It probes each enabled adapter, fetches the ref under observation,
pushes the adapter's producer *inputs* through the ordinary ingestion gateway, runs the
deterministic critic pack against the events it just accepted, and appends one audit entry per
adapter. It never files a rebuttal, never records a disposition, and never changes a lifecycle
state: findings it produces are ordinary ``detected`` findings that a human still closes
(ADR 0004, SPEC §6).

Three rules this module exists to make enforceable:

* **A cycle never raises.** An adapter that fails is a degraded outcome with the failure in
  ``detail``; it is not an exception that escapes the cycle. One failure must not kill the
  loop, so :meth:`WatchCycle.run_once` cannot raise for anything an adapter can do.
* **An error is not an empty observation.** ``fetch_for_ref`` raises ``GitCiError`` by design
  (``src/nyx/adapters/git_ci/adapter.py``). The cycle catches it and records ``DEGRADED`` --
  "the adapter is down" is a fact about the deployment, not a statement that the repository
  was empty.
* **The audit actor is a service, never a human.** ``nyx-watch`` is a deterministic principal
  of kind ``service``; no operator identity is invented, so an unattended writer cannot be
  mistaken for a person who made a decision.

``detail`` is composed from identifiers, counts, and adapter-authored health prose only --
never from ``str(exception)``, which can echo a request URL carrying a credential.

Adapter construction stays outside this module
----------------------------------------------
``WatchCycle`` receives an object that can *enumerate* the enabled adapters; it does not import
``nyx.adapters`` and does not build a registry. Gate G7
(``tests/contract/test_adapter_registry.py``) allows only ``api/app.py`` and ``cli/main.py`` to
import the adapter package, so the watcher is wired by its caller: ``nyx watch`` passes the
registry built by :func:`nyx.adapters.factory.build_adapter_registry` from the same settings the
cycle holds. That keeps the watcher and ``nyx doctor`` observing the identical set from the
identical settings (ADR 0009) without making the watcher a third importer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import nyx.critics  # noqa: F401 - importing the package registers the deterministic critic pack
from nyx.audit.writer import AuditEntryInput, AuditWriter
from nyx.config import Settings
from nyx.domain.base import Identifier, NyxModel, PrincipalRef, UtcDatetime, utc_now
from nyx.domain.enums import AuditOutcome, CriticResultStatus, PrincipalKind
from nyx.findings.service import FindingService
from nyx.ingestion.errors import IngestResult
from nyx.ingestion.inputs import ClaimInput, EventInput, EvidenceInput
from nyx.ingestion.service import IngestionService
from nyx.storage.db import SessionFactory, session_scope

#: The audit action every cycle appends, once per adapter. ``ActionName``'s pattern requires a
#: dot-separated lowercase name, and ``nyx audit verify`` verifies it like any other entry.
CYCLE_ACTION = "watch.cycle"

#: The watcher's own service identity. A constant: the watcher does not invent an operator, and
#: a deterministic principal is what makes an unattended writer distinguishable from a human.
WATCHER_PRINCIPAL = PrincipalRef(
    id="nyx-watch", kind=PrincipalKind.SERVICE, display_name="NYX watcher"
)

#: Ceilings for the two free-text strings a cycle writes. ``AdapterHealth.detail`` is capped by
#: its own model at 1000; the cycle caps it again so an adapter-authored string cannot push the
#: outcome's ``detail`` past the audit entry's 2000-character ``reason`` field.
_DETAIL_PROSE_LIMIT = 500
_SUMMARY_LIMIT = 2000

#: Notes that mean the cycle did not complete what it set out to do. The outcome's state is
#: derived from these, so a cycle can only report ``OK`` when every step finished.
_DEGRADED_NOTE_PREFIXES = (
    "not fetched",
    "not ingested",
    "the health probe raised",
    "the adapter could not be resolved",
)

#: The wire value of a healthy probe. ``AdapterState.OK`` is the adapter package's vocabulary;
#: this module compares against the string so it does not need to import it.
_OK_STATE = "ok"


class MissingAdapterInventoryError(ValueError):
    """Raised when a cycle is constructed without the adapter inventory it must iterate.

    A typed error rather than a silent empty registry: "the watcher has no adapters" is a
    configuration fact the caller needs verbatim, and failing closed here is what keeps a
    misconfigured ``nyx watch`` from reporting a clean cycle over nothing (ADR 0009).
    """


def _degraded_notes(notes: list[str]) -> bool:
    """Return whether any step of the cycle did not complete."""

    return any(note.startswith(_DEGRADED_NOTE_PREFIXES) for note in notes)


@runtime_checkable
class AdapterHealthView(Protocol):
    """The two fields of a probe result the cycle reads."""

    state: str
    detail: str


@runtime_checkable
class ProducerAdapter(Protocol):
    """The structural half of the adapter contract this module needs.

    Deliberately narrower than ``nyx.adapters.base.Adapter``: the cycle probes and fetches, so it
    needs ``adapter_id``, ``health()``, and ``fetch_for_ref()`` and nothing else. A real adapter
    satisfies it structurally, so nothing here constructs or imports one.
    """

    adapter_id: str
    version: str

    def health(self) -> AdapterHealthView: ...

    def fetch_for_ref(self, ref: str) -> list[Any]: ...


@runtime_checkable
class AdapterInventory(Protocol):
    """What the cycle needs from the caller's registry: the adapters the deployment enabled."""

    def enabled(self) -> Iterable[ProducerAdapter]: ...


class CycleOutcome(NyxModel):
    """The audited result of running one adapter once (ADR 0009).

    ``fetched`` counts the producer inputs the adapter returned. ``ingested`` counts the ones
    the gateway accepted as new rows; ``duplicates`` counts the ones it recognised as replays.
    Rejections are reported in ``detail`` rather than folded into ``fetched``, which would
    overstate what the adapter observed.

    ``state`` carries the adapter-package vocabulary (``ok`` / ``degraded``) as a plain string so
    this module does not import ``nyx.adapters``.

    ``detail`` is human-readable and carries no credential by construction.
    """

    adapter_id: Identifier
    state: str
    fetched: int
    ingested: int
    duplicates: int
    findings: int
    started_at: UtcDatetime
    completed_at: UtcDatetime
    detail: str


class WatchCycle:
    """Run one read-only, idempotent pass over the enabled adapters.

    ``inventory`` supplies the adapters to iterate. In production the caller passes the registry
    built by :func:`nyx.adapters.factory.build_adapter_registry` -- the same registry
    ``nyx doctor`` reports -- so the watcher observes exactly the adapters the deployment
    enabled and nothing else (ADR 0009). The watcher cannot build that registry itself: gate G7
    (``tests/contract/test_adapter_registry.py``) allows only ``api/app.py`` and ``cli/main.py``
    to import ``nyx.adapters``, so ``nyx watch`` is the caller that wires them together.

    Omitting ``inventory`` is a **refusal**, not an empty default. A watcher that silently built
    its own empty registry would report a clean cycle over nothing, which ADR 0009 forbids, so
    the constructor names the missing argument instead.

    ``clock`` is injectable so a test can hold a cycle's two timestamps still and assert their
    relationship instead of asserting wall-clock readings.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        settings: Settings,
        clock: Callable[[], datetime] = utc_now,
        inventory: AdapterInventory | None = None,
    ) -> None:
        if inventory is None:
            raise MissingAdapterInventoryError(
                "WatchCycle requires an adapter inventory: pass the registry built by "
                "nyx.adapters.factory.build_adapter_registry(settings). A watcher with no "
                "adapters would fetch nothing and report success, which ADR 0009 forbids."
            )
        self._session_factory = session_factory
        self._settings = settings
        self._inventory = inventory
        self._clock = clock
        self._logger = logging.getLogger("nyx.watch")
        self._cycle = 0
        self._ingest = IngestionService(session_factory, settings)
        self._findings = FindingService(session_factory)

    # -- cycle ------------------------------------------------------------

    def run_once(self) -> list[CycleOutcome]:
        """Run one cycle and return one :class:`CycleOutcome` per enabled adapter.

        Never raises. A fetch that raises is caught inside :meth:`_run_adapter`, and so is any
        other exception an adapter can produce: one failure must not stop the next adapter.
        """

        self._cycle += 1
        request_id = f"watch-{self._cycle}"

        outcomes: list[CycleOutcome] = []
        for adapter in self._inventory.enabled():
            outcomes.append(self._run_adapter(adapter, request_id=request_id))
        return outcomes

    # -- one adapter ------------------------------------------------------

    def _run_adapter(self, adapter: ProducerAdapter, *, request_id: str) -> CycleOutcome:
        """Probe, fetch, ingest, criticise, and audit one adapter. Never raises."""

        adapter_id = adapter.adapter_id
        started_at = self._clock()
        counts = {"fetched": 0, "ingested": 0, "duplicates": 0, "findings": 0}
        notes: list[str] = []

        health = self._probe(adapter, notes=notes)
        if health.state != _OK_STATE:
            # A degraded adapter is not fetched: its own health probe is the smallest honest
            # proof that it can observe anything, and fetching past a failed probe would report
            # "nothing seen" where the truth is "could not look".
            notes.append("not fetched: the health probe did not report ok")
            return self._finish(adapter_id, started_at, counts, health, notes, request_id)

        inputs = self._fetch(adapter, counts=counts, notes=notes)
        if inputs is None:
            # The fetch raised. It is recorded as an error, not as an empty observation, so
            # the probe's own (healthy) reading is reported alongside the failure.
            notes.append("not ingested: the fetch failed")
            return self._finish(adapter_id, started_at, counts, health, notes, request_id)

        event_ids = self._ingest_inputs(inputs, request_id=request_id, counts=counts, notes=notes)
        for event_id in event_ids:
            counts["findings"] += self._criticise(event_id, notes=notes)
        if event_ids:
            notes.append(f"ran the deterministic critic pack over {len(event_ids)} new event(s)")

        return self._finish(adapter_id, started_at, counts, health, notes, request_id)

    # -- steps ------------------------------------------------------------

    def _probe(self, adapter: ProducerAdapter, *, notes: list[str]) -> AdapterHealthView:
        """Probe one adapter.

        ``health()`` is documented never to raise, and the cycle does not rely on that: a probe
        that raises is a degraded adapter, not a dead cycle. Only the exception's *type* is
        reported, never its message, because a message can echo a request URL.
        """

        try:
            return adapter.health()
        except Exception as exc:  # noqa: BLE001 - the guard must not raise either
            notes.append(
                f"the health probe raised {type(exc).__name__} and was reported as degraded; "
                "the exception message is not included because it can echo a request URL "
                "carrying a credential"
            )
            return _UnprobedHealth(
                state="degraded", detail=f"health probe raised {type(exc).__name__}"
            )

    def _fetch(
        self, adapter: ProducerAdapter, *, counts: dict[str, int], notes: list[str]
    ) -> list[Any] | None:
        """Fetch the configured ref. ``None`` means the adapter raised.

        A ``GitCiError``'s message is adapter-authored prose built from a queryless URL or a
        type name, so it is the one exception string safe to quote. Any other exception is
        reported by type name only, for the same reason the adapter itself does.
        """

        try:
            fetched = list(adapter.fetch_for_ref(self._settings.watch_ref))
        except Exception as exc:  # noqa: BLE001 - an error is not an empty observation
            if type(exc).__name__ == "GitCiError":
                notes.append(f"fetch raised GitCiError: {exc}")
            else:
                notes.append(f"fetch raised {type(exc).__name__} and did not reach the adapter")
            self._logger.warning(
                "watch: fetch from %s failed (%s)", adapter.adapter_id, type(exc).__name__
            )
            return None

        counts["fetched"] = len(fetched)
        notes.append(
            f"fetched {len(fetched)} producer input(s) for ref {self._settings.watch_ref!r}"
        )
        return fetched

    def _ingest_inputs(
        self,
        inputs: list[Any],
        *,
        request_id: str,
        counts: dict[str, int],
        notes: list[str],
    ) -> list[str]:
        """Push every producer input through the ordinary gateway. Returns new event ids.

        The adapter's output is not special-cased in any way: it goes through the same
        redaction, schema validation, identity deduplication, and per-record audit that a human
        submission gets (ADR 0008). A per-item failure is counted, not raised -- one bad record
        must not fail the batch.
        """

        created_events: list[str] = []
        for position, item in enumerate(inputs):
            try:
                result = self._ingest_one(item, request_id=request_id, position=position)
            except Exception as exc:  # noqa: BLE001 - one bad item must not fail the batch
                notes.append(f"rejected: {type(item).__name__} ({type(exc).__name__})")
                continue
            if result.status == "created":
                counts["ingested"] += 1
                notes.append(f"accepted: {type(item).__name__} -> {result.record_id}")
                if isinstance(item, EventInput) and result.record_id is not None:
                    created_events.append(result.record_id)
            elif result.status == "duplicate":
                counts["duplicates"] += 1
                notes.append(f"duplicate: {type(item).__name__} -> {result.record_id}")
            else:
                code = result.error.code if result.error is not None else "unknown"
                notes.append(f"rejected: {type(item).__name__} ({code})")
        return created_events

    def _ingest_one(self, item: Any, *, request_id: str, position: int) -> IngestResult:
        """Ingest one producer input, naming it so the audit trail is attributable."""

        item_request_id = f"{request_id}-{position}"
        if isinstance(item, EventInput):
            return self._ingest.ingest_event(item, request_id=item_request_id)
        if isinstance(item, ClaimInput):
            return self._ingest.register_claim(item, request_id=item_request_id)
        if isinstance(item, EvidenceInput):
            return self._ingest.register_evidence(item, request_id=item_request_id)
        raise TypeError(f"unsupported adapter input {type(item).__name__}")

    def _criticise(self, event_id: str, *, notes: list[str]) -> int:
        """Run the deterministic critic pack against one event. Returns the finding count.

        A critic failure is recorded by the registry as an ``error`` run, not raised, so the
        pack cannot take the cycle down either.
        """

        produced = 0
        for outcome in self._findings.run_critics(event_id):
            if outcome.result is CriticResultStatus.FINDING and outcome.finding_id is not None:
                produced += 1
                notes.append(f"finding: {outcome.critic_id} -> {outcome.finding_id}")
        return produced

    # -- outcome and audit ------------------------------------------------

    def _finish(
        self,
        adapter_id: str,
        started_at: datetime,
        counts: dict[str, int],
        health: AdapterHealthView,
        notes: list[str],
        request_id: str,
    ) -> CycleOutcome:
        """Build the outcome and append exactly one audit entry for this adapter.

        The audit entry is written even for a failed adapter: an unattended process that
        writes nothing to the audit trail when it fails is exactly what NYX exists to catch.
        A failure to *write* the audit entry must not raise either -- it is logged, and the
        outcome still reports the adapter's real state.

        The state is derived from what the cycle actually did, not from a flag: ``notes``
        records every step that did not complete, and a cycle with any such note is degraded.
        """

        completed_at = self._clock()
        failed = _degraded_notes(notes)
        outcome = CycleOutcome(
            adapter_id=adapter_id,
            state="degraded" if failed else _OK_STATE,
            fetched=counts["fetched"],
            ingested=counts["ingested"],
            duplicates=counts["duplicates"],
            findings=counts["findings"],
            started_at=started_at,
            completed_at=completed_at,
            detail=self._detail(adapter_id, notes, health),
        )
        self._audit(outcome, request_id=request_id)
        return outcome

    def _audit(self, outcome: CycleOutcome, *, request_id: str) -> None:
        """Append the one ``watch.cycle`` entry for this adapter's cycle.

        The actor is the watcher's own service principal. The reason names the adapter, the ref
        under observation, the counts, and the outcome, so ``nyx audit verify`` and a human
        reading the chain can reconstruct the cycle from the entry alone.
        """

        duration = outcome.completed_at - outcome.started_at
        reason = (
            f"watch cycle {self._cycle} adapter={outcome.adapter_id} "
            f"ref={self._settings.watch_ref!r} outcome={outcome.state} "
            f"fetched={outcome.fetched} ingested={outcome.ingested} "
            f"duplicates={outcome.duplicates} findings={outcome.findings} "
            f"duration_ms={duration // timedelta(milliseconds=1)}"
        )
        entry = AuditEntryInput(
            actor=WATCHER_PRINCIPAL,
            action=CYCLE_ACTION,
            target_type="adapter",
            target_id=outcome.adapter_id,
            request_id=request_id,
            outcome=AuditOutcome.SUCCESS if outcome.state == _OK_STATE else AuditOutcome.FAILURE,
            reason=reason[:_SUMMARY_LIMIT],
        )
        try:
            with session_scope(self._session_factory) as session:
                AuditWriter(session).append(entry)
        except Exception as exc:  # noqa: BLE001 - an audit write failure is not a cycle failure
            self._logger.error(
                "watch cycle %s: could not append the audit entry for %s (%s)",
                request_id,
                outcome.adapter_id,
                type(exc).__name__,
            )

    @staticmethod
    def _detail(adapter_id: str, notes: list[str], health: AdapterHealthView) -> str:
        """Compose ``detail`` from identifiers, counts, and bounded adapter prose.

        Health prose is capped and whitespace-collapsed: it is the one string an adapter authors
        itself, and a hostile or buggy adapter must not be able to overflow the model field or
        smuggle a multi-line payload into the outcome.
        """

        parts = [f"{adapter_id}: {health.state}"]
        if health.detail:
            parts.append(re.sub(r"\s+", " ", health.detail).strip()[:_DETAIL_PROSE_LIMIT])
        parts.extend(notes)
        return "; ".join(part for part in parts if part)[:_SUMMARY_LIMIT]


class _UnprobedHealth:
    """A ``DEGRADED`` reading for a probe that could not run at all."""

    __slots__ = ("state", "detail")

    def __init__(self, *, state: str, detail: str) -> None:
        self.state = state
        self.detail = detail


__all__ = [
    "CYCLE_ACTION",
    "CycleOutcome",
    "MissingAdapterInventoryError",
    "WATCHER_PRINCIPAL",
    "WatchCycle",
]
