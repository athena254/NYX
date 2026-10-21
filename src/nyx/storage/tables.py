"""SQLAlchemy declarative tables mapped 1:1 onto the domain aggregates.

Enumerations are stored as their string values. Structured fields (system metadata, event
payloads, claim scopes, lineage lists) use JSON columns. Columns that the API will filter on
are indexed.

The dispute tables (``rebuttals``, ``dispositions``, ``verification_jobs``, ``policy_versions``)
are **append-only**: no repository exposes an update or delete for them, because the SPEC §5.7
contract is that a disposition supersedes its predecessor by linking to it and never rewrites it.
A new row is the only supported way to record a change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all NYX tables."""


class SystemRow(Base):
    __tablename__ = "monitored_systems"
    __table_args__ = (
        UniqueConstraint("name", "environment", name="uq_monitored_systems_name_environment"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    adapter: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    source_system_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    parent_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    subject: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_ref: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    trust_class: Mapped[str] = mapped_column(String(32), nullable=False)
    labels: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    retention_class: Mapped[str] = mapped_column(String(32), nullable=False)
    # Retention markers (ADR 0003, privacy-and-retention §5). These are the only columns a
    # tombstone may write; ``redacted_at`` is null until the record is redacted.
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redacted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    redaction_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    redaction_action: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ClaimRow(Base):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    statement: Mapped[str] = mapped_column(String(2000), nullable=False)
    claimant_system_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    claimant_principal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    source_event_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    retention_class: Mapped[str] = mapped_column(String(32), nullable=False)
    # Retention markers (ADR 0003, privacy-and-retention §5); see ``EventRow``.
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redacted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    redaction_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    redaction_action: Mapped[str | None] = mapped_column(String(32), nullable=True)


class EvidenceRow(Base):
    __tablename__ = "evidence_items"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    source_system_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_ref: Mapped[str] = mapped_column(String(2048), nullable=False)
    locator: Mapped[str | None] = mapped_column(String(512), nullable=True)
    scope: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    integrity_method: Mapped[str] = mapped_column(String(32), nullable=False)
    producer_identity: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    relevance_to_claim: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    relevance_to_finding: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    relevance_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    limitations: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    access_policy_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    retention_class: Mapped[str] = mapped_column(String(32), nullable=False)
    # Retention markers (ADR 0003, privacy-and-retention §5); see ``EventRow``.
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redacted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    redaction_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    redaction_action: Mapped[str | None] = mapped_column(String(32), nullable=True)


class IdempotencyRow(Base):
    """A reserved dedupe key mapping to the record it produced (SPEC §11.2).

    Identity-based only: the composite key is ``(scope, dedupe_key)`` where ``dedupe_key`` is a
    producer event id or idempotency key. ``request_hash`` records what the key was first
    accepted with so a later reuse with different content can be detected.
    """

    __tablename__ = "idempotency_records"

    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class AuditRow(Base):
    __tablename__ = "audit_entries"
    __table_args__ = (UniqueConstraint("sequence", name="uq_audit_entries_sequence"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    prior_entry_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(80), nullable=False)


class FindingRow(Base):
    """A composed finding (SPEC §5.5). Observations, inferences, recommendations, and
    unknowns are stored as separate JSON structures and never collapsed into one prose field.
    """

    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(String(4000), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    confidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    scope_system_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    affected_system_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    claim_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    event_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    counterevidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reasoning: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    limitations: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    unknowns: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    recommended_action: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    dedupe_rationale: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    duplicate_of: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CriticRunRow(Base):
    """One critic execution (SPEC §5.6). ``finding_id`` links a run to its composed finding."""

    __tablename__ = "critic_runs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    critic_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    critic_type: Mapped[str] = mapped_column(String(32), nullable=False)
    critic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    input_hashes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    rationale: Mapped[str] = mapped_column(String(2000), nullable=False)
    tool_run_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    resource_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_details: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    finding_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)


class RebuttalRow(Base):
    """A challenge to a finding (SPEC §5.7). Append-only.

    ``evidence_ids`` is the counterevidence the submitter attaches. It is stored on the rebuttal
    because the challenge is what introduces it; the finding's own ``counterevidence_ids`` is the
    derived view the critics read.
    """

    __tablename__ = "rebuttals"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    finding_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    argument: Mapped[str] = mapped_column(String(4000), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    requested_outcome: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class VerificationJobRow(Base):
    """One independent verification attempt (SPEC §5.7, §7 step 8). Append-only.

    ``independence`` records how the check relates to the critic that raised the finding. It
    is a first-class column rather than a comment because "no independent method exists" is a
    limitation a reader must be able to query, not a footnote they might miss.
    """

    __tablename__ = "verification_jobs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    finding_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    check_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    independence: Mapped[str] = mapped_column(String(2000), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    tool_run_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    failure_details: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DispositionRow(Base):
    """The authorised outcome of a finding (SPEC §5.7). Append-only, supersede-by-link.

    ``supersedes_disposition_id`` is the chain link. It is indexed because reconstructing the chain
    is the primary read pattern, and the current disposition is the row that nothing supersedes.
    """

    __tablename__ = "dispositions"
    # A disposition may be superseded at most once: the chain must never branch. NULLs compare as
    # distinct, so this does not (and cannot) constrain how many dispositions are first in a chain.
    __table_args__ = (
        UniqueConstraint(
            "finding_id", "supersedes_disposition_id", name="uq_dispositions_superseded_once"
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    finding_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    rationale: Mapped[str] = mapped_column(String(4000), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    verification_run_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    policy_version: Mapped[str] = mapped_column(String(256), nullable=False)
    supersedes_disposition_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )


class PolicyVersionRow(Base):
    """One recorded version of a policy rule, and who changed it (acceptance scenario 22).

    Append-only by construction: registering a new version inserts a row. The ``(rule_id, version)``
    pair is unique so the same version cannot be recorded twice under different change reasons.
    """

    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint("rule_id", "version", name="uq_policy_versions_rule_version"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(256), nullable=False)
    digest: Mapped[str] = mapped_column(String(97), nullable=False)
    changed_by: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    change_reason: Mapped[str] = mapped_column(String(4000), nullable=False)
