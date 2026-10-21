"""Retention policy and the redaction workflow (Phase 4).

Implements ``docs/privacy-and-retention.md`` §4–§5: class-bounded retention, a purge that
tombstones payloads rather than deleting records, and a redaction workflow that preserves the
fact of a change.

Scope limit that matters: redaction markers live on ``events``, ``claims``, and
``evidence_items`` only. Findings, rebuttals, dispositions, and policy versions are append-only
(ADR 0003) and are never rewritten — a disposition that relied on redacted evidence records that
the evidence was redacted and remains contestable.
"""
