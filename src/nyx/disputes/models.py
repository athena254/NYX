"""Rebuttal, verification job, disposition, and policy version models (SPEC §5.7, §7).

Every model in this module is **append-only by construction**: the fields are the recorded facts of
an event that happened, and none of them is ever mutated by a later operation. A new disposition
supersedes the previous one by carrying its id in ``supersedes_disposition_id``; the previous row is
left exactly as it was. History is therefore reconstructable from the rows alone.

``Rebuttal`` and ``Disposition`` match the checked-in Phase 0 JSON Schemas field for field, and
``tests/contract/test_schema_agreement.py`` proves that agreement. ``VerificationJob`` and
``PolicyVersion`` have no checked-in schema yet; they are defined here to the same conventions.

Design note on timestamps
-------------------------
``submitted_at`` and ``decided_at`` are required rather than defaulted. They are facts about when a
recorded human or service acted, and the service supplies them from an injectable clock so tests can
assert them. A default would hide a bug where the service forgot to record the real time.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from nyx.domain.base import (
    Digest,
    Identifier,
    NyxModel,
    SchemaVersion,
    UtcDatetime,
)
from nyx.domain.enums import (
    DispositionOutcome,
    RebuttalRequestedOutcome,
    VerificationJobStatus,
    VerificationOutcome,
)
from nyx.domain.ids import (
    new_disposition_id,
    new_policy_version_id,
    new_rebuttal_id,
    new_verification_job_id,
)

ArgumentText = Annotated[str, StringConstraints(min_length=1, max_length=4000)]
PolicyVersionText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
CheckIdText = Annotated[
    str,
    StringConstraints(min_length=3, max_length=128, pattern=r"^[a-z0-9]+(\.[a-z0-9][a-z0-9_-]*)+$"),
]


class Rebuttal(NyxModel):
    """A challenge to a finding (SPEC §5.7).

    ``requested_outcome`` is what the submitter wants, not what happened. It is a request, so it is
    not a verdict and must never be read as one. ``no_change`` is an explicit request to leave the
    finding standing, which is a legitimate challenge ("we disagree, and here is why the finding
    should stay").
    """

    id: Identifier = Field(default_factory=new_rebuttal_id)
    schema_version: SchemaVersion = "1.0"
    finding_id: Identifier
    submitted_by: Identifier
    submitted_at: UtcDatetime
    argument: ArgumentText
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    requested_outcome: RebuttalRequestedOutcome


class VerificationJob(NyxModel):
    """An independent attempt to confirm or refute a finding (SPEC §5.7, §7 step 8).

    Verification is deliberately *not* a re-run of the critic that raised the finding: an evaluator
    that shares the original's blind spot cannot corroborate it. ``independence`` records how the
    check relates to the originating critic, and when no independent method exists the job records
    that limitation instead of implying corroboration.

    A job whose check is not on the allowlist is ``refused`` and never executes. That is an outcome,
    not an error, so "we declined to run this" stays distinguishable from "we ran it and it failed".
    """

    id: Identifier = Field(default_factory=new_verification_job_id)
    finding_id: Identifier
    check_id: CheckIdText
    requested_by: Identifier
    requested_at: UtcDatetime
    status: VerificationJobStatus = VerificationJobStatus.QUEUED
    outcome: VerificationOutcome | None = None
    result_summary: ArgumentText | None = None
    independence: Annotated[str, StringConstraints(min_length=1, max_length=2000)] = (
        "no independent method exists; this check does not corroborate the finding"
    )
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    tool_run_refs: list[Annotated[str, StringConstraints(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=64
    )
    failure_details: ArgumentText | None = None
    started_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None


class Disposition(NyxModel):
    """The authorised outcome of a finding (SPEC §5.7).

    ``supersedes_disposition_id`` is the append-only link: the first disposition for a finding has
    ``None``, and every later one names the disposition it replaces. The previous row is never
    rewritten, so the full chain is reconstructable and auditable.

    ``policy_version`` records which version of the policy decided this, so a later policy change
    cannot retroactively alter what was decided.
    """

    id: Identifier = Field(default_factory=new_disposition_id)
    schema_version: SchemaVersion = "1.0"
    finding_id: Identifier
    outcome: DispositionOutcome
    rationale: ArgumentText
    decided_by: Identifier
    decided_at: UtcDatetime
    verification_run_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    policy_version: PolicyVersionText
    supersedes_disposition_id: Identifier | None


class PolicyVersion(NyxModel):
    """One recorded version of a policy rule, and who changed it (acceptance scenario 22).

    The row is the audit record of a policy *change*: it carries the version, the actor, the
    time, the reason, and a content digest of the rule so a later reader can tell whether the
    rule they are looking at is the one that was in force. A new version retains the old row.
    """

    id: Identifier = Field(default_factory=new_policy_version_id)
    rule_id: Identifier
    version: PolicyVersionText
    digest: Digest
    changed_by: Identifier
    changed_at: UtcDatetime
    change_reason: ArgumentText
