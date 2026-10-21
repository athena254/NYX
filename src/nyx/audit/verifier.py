"""Verification of the audit hash chain (SPEC §5.8, acceptance scenario 17).

The verifier walks the stored entries in ``sequence`` order and checks three things:

* the sequence starts at 1 and increments by exactly 1;
* each entry's ``prior_entry_hash`` equals the previous entry's ``entry_hash``;
* each entry's ``entry_hash`` recomputes from its stored fields.

It reports the first divergence and stops. An empty store is a clean chain.

Documented non-claim
--------------------
This detects accidental mutation, partial writes, and out-of-order edits within the stored
sequence. It does **not** defeat a privileged attacker who can rewrite the whole database and
recompute every hash (SPEC §5.8, ADR 0003). It is not a signature and not a transparency log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from nyx.audit.chain import entry_hash
from nyx.domain.audit import AuditEntry
from nyx.storage.db import SessionFactory, session_scope
from nyx.storage.repositories.audit import AuditRepository

NON_CLAIM = (
    "The audit hash chain detects accidental mutation, partial writes, and out-of-order edits "
    "within the stored sequence. It does not defeat a privileged attacker who can rewrite the "
    "whole database and recompute every hash (SPEC §5.8, ADR 0003)."
)

_PAGE_SIZE = 500


@dataclass(frozen=True)
class Divergence:
    """The first place the stored chain stops matching its own hashes."""

    sequence: int
    entry_id: str
    reason: str


@dataclass(frozen=True)
class VerificationReport:
    """The outcome of walking the whole chain."""

    ok: bool
    entries_checked: int
    first_divergence: Divergence | None

    @property
    def non_claim(self) -> str:
        """The threat-model boundary this report does not cross."""

        return NON_CLAIM

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation for the CLI and logs."""

        return {
            "ok": self.ok,
            "entries_checked": self.entries_checked,
            "first_divergence": (
                None
                if self.first_divergence is None
                else {
                    "sequence": self.first_divergence.sequence,
                    "entry_id": self.first_divergence.entry_id,
                    "reason": self.first_divergence.reason,
                }
            ),
            "non_claim": self.non_claim,
        }


class AuditVerifier:
    """Recompute the audit chain from the stored rows."""

    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository

    def verify(self) -> VerificationReport:
        entries = self._all_entries()
        previous_hash: str | None = None
        checked = 0

        for position, entry in enumerate(entries, start=1):
            if entry.sequence != position:
                reason = "not_genesis" if position == 1 else "sequence_gap"
                return self._diverged(entry, reason, checked)

            if entry.prior_entry_hash != previous_hash:
                return self._diverged(entry, "prior_hash_mismatch", checked)

            if (
                entry_hash(
                    schema_version=entry.schema_version,
                    sequence=entry.sequence,
                    actor=entry.actor,
                    action=entry.action,
                    target=entry.target,
                    timestamp=entry.timestamp,
                    request_id=entry.request_id,
                    correlation_id=entry.correlation_id,
                    outcome=entry.outcome,
                    reason=entry.reason,
                    prior_entry_hash=entry.prior_entry_hash,
                )
                != entry.entry_hash
            ):
                return self._diverged(entry, "recomputed_hash_mismatch", checked)

            previous_hash = entry.entry_hash
            checked += 1

        return VerificationReport(ok=True, entries_checked=checked, first_divergence=None)

    def _all_entries(self) -> list[AuditEntry]:
        """Read the whole chain in sequence order, page by page.

        ``AuditRepository.list`` orders by sequence but defaults to a 100-row page; a verifier
        that inherited that limit would silently check only the first 100 entries.
        """

        entries: list[AuditEntry] = []
        offset = 0
        while True:
            page = self._repository.list(limit=_PAGE_SIZE, offset=offset)
            entries.extend(page)
            if len(page) < _PAGE_SIZE:
                return entries
            offset += _PAGE_SIZE

    @staticmethod
    def _diverged(entry: AuditEntry, reason: str, checked: int) -> VerificationReport:
        return VerificationReport(
            ok=False,
            entries_checked=checked,
            first_divergence=Divergence(
                sequence=entry.sequence,
                entry_id=entry.id,
                reason=reason,
            ),
        )


def verify_chain(session_factory: SessionFactory) -> VerificationReport:
    """Verify the chain over one read transaction."""

    with session_scope(session_factory) as session:
        return AuditVerifier(AuditRepository(session)).verify()


def verify_on_startup(
    session_factory: SessionFactory, *, logger: logging.Logger | None = None
) -> VerificationReport:
    """Verify the chain at startup and log a warning on divergence.

    A divergence does not raise and does not refuse to boot: it is a finding for an authorised
    human, not a reason to take the service down. ``src/nyx/api/app.py`` (orchestrator-owned) calls
    this when ``settings.audit_verify_on_startup`` is true.
    """

    log = logger or logging.getLogger("nyx.audit")
    report = verify_chain(session_factory)
    if not report.ok:
        divergence = report.first_divergence
        assert divergence is not None  # nosec B101 - ok=False implies a divergence
        log.warning(
            "audit chain diverged at sequence %s (%s): %s; %s",
            divergence.sequence,
            divergence.entry_id,
            divergence.reason,
            report.non_claim,
        )
    return report
