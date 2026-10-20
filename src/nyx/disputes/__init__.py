"""Dispute protocol: rebuttals, independent verification, and dispositions (SPEC §5.7, §6).

This package owns the append-only workflow that drives the finding lifecycle. A rebuttal
challenges a finding; a verification job independently tests the proposition; a disposition
records the authorised outcome. Nothing here deletes or rewrites history -- every record is
appended, and a disposition supersedes its predecessor by linking to it rather than replacing it.

The lifecycle state machine in :mod:`nyx.findings.lifecycle` remains the only path that changes a
finding's status. This package calls it; it does not duplicate it.
"""

from __future__ import annotations
