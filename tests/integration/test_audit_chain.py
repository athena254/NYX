"""Characterization tests for the audit writer's hash chain.

Written *before* the hash function moved out of ``writer.py``. There was no prior test of the
chain itself, so these assertions pin the exact digest of a known entry set: if the extraction in
slice 4.2 changes one byte of the hashed payload, this file goes red.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.audit.writer import AuditEntryInput, AuditWriter
from nyx.domain.base import PrincipalRef
from nyx.domain.enums import AuditOutcome, PrincipalKind
from nyx.storage.db import SessionFactory, session_scope

_ACTOR = PrincipalRef(id="principal_operator", kind=PrincipalKind.HUMAN)
_FIRST_TIMESTAMP = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
_SECOND_TIMESTAMP = datetime(2026, 10, 21, 12, 5, tzinfo=UTC)

_FIRST_HASH = "sha256:2651d2e19c3e4ac789e11028c8ef572a528a406f9c451d3d1900057cad2f8eaa"
_SECOND_HASH = "sha256:8d76fa71afe1036ed74ce348fdcfe95ee1c305f8875c47d73f0a52f418243fd3"


def _first_entry() -> AuditEntryInput:
    return AuditEntryInput(
        schema_version="1.0",
        actor=_ACTOR,
        action="system.registered",
        target_type="monitored_system",
        target_id="sys_characterization",
        request_id="req-audit-chain-1",
        correlation_id=None,
        outcome=AuditOutcome.SUCCESS,
        reason="characterization entry",
        timestamp=_FIRST_TIMESTAMP,
    )


def _second_entry() -> AuditEntryInput:
    return AuditEntryInput(
        schema_version="1.0",
        actor=_ACTOR,
        action="system.registration_replayed",
        target_type="monitored_system",
        target_id="sys_characterization",
        request_id="req-audit-chain-2",
        correlation_id="corr-audit-chain",
        outcome=AuditOutcome.SUCCESS,
        reason="characterization replay",
        timestamp=_SECOND_TIMESTAMP,
    )


def test_writer_entry_hash_is_stable_for_a_known_entry(session_factory: SessionFactory) -> None:
    with session_scope(session_factory) as session:
        first = AuditWriter(session).append(_first_entry())

    assert first.sequence == 1
    assert first.prior_entry_hash is None
    assert first.entry_hash == _FIRST_HASH


def test_writer_links_each_entry_to_the_previous_hash(session_factory: SessionFactory) -> None:
    with session_scope(session_factory) as session:
        writer = AuditWriter(session)
        first = writer.append(_first_entry())
        second = writer.append(_second_entry())

    assert second.sequence == 2
    assert second.prior_entry_hash == first.entry_hash
    assert second.entry_hash == _SECOND_HASH
