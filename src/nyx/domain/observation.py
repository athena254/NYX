"""The observation aggregate — what NYX itself gathered about the world.

An observation is the ground-truth counterpart of a claim. A claim is what an agent or system
*asserts*; an observation is what NYX *saw*. The two are stored in different records on purpose:
keeping them apart is what makes "the booking exists in the database" checkable independently of
"the agent said it booked".

This module deliberately contains no storage access and no probe execution. It is the data shape
only; probes live in :mod:`nyx.evidence.probes` and persistence in
:mod:`nyx.storage.repositories.observations`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, StringConstraints

from nyx.domain.base import (
    Digest,
    Identifier,
    NyxModel,
    PrincipalRef,
    SchemaVersion,
    Scope,
    UtcDatetime,
)
from nyx.domain.enums import Environment, ProbeKind, ProbeOutcome, VerificationStrength
from nyx.domain.ids import new_observation_id


class Observation(NyxModel):
    """One thing NYX observed about a subject, by a probe it ran itself.

    ``subject_version`` is the commit, build hash, config hash, or deployment id the observation
    applies to. It is the field that lets a critic bind an observation to the claim it is offered
    as support for: an observation of commit ``def456`` cannot verify a claim about ``abc123``.

    ``read_only`` is the safety flag, not a description. A probe that reports ``False`` mutated
    something, and may only have run behind the mutating-probe gate.

    ``result`` is the probe's own outcome, and ``verification_strength`` is what that outcome is
    *worth*. A ``state_check`` that reads the live database is worth
    ``independently_observed_end_state``; a ``reexecution`` is worth ``rerun_by_nyx``. The
    mapping lives in :mod:`nyx.evidence.verification` so every critic and every probe agrees on
    it, rather than each deciding for itself.
    """

    id: Identifier = Field(default_factory=new_observation_id)
    schema_version: SchemaVersion = "1.0"
    probe_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    probe_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    subject: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    subject_version: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    collected_at: UtcDatetime
    collector: PrincipalRef
    method: ProbeKind
    result: ProbeOutcome
    raw_artifact_hash: Digest | None = None
    environment: Environment
    read_only: bool = True
    verification_strength: VerificationStrength
    scope: Scope | None = None
    #: The claim this observation was collected to test, when it was collected against one.
    #: An observation gathered before any claim exists carries ``None`` and a ``note``.
    relevance_to_claim: Identifier | None = None
    #: The free-text finding of the probe: "row booking_17 exists with status=confirmed".
    detail: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    #: Structured probe output (the row, the file listing, the parsed log lines). Untrusted
    #: producer data: treated as display text, never as an instruction.
    payload: dict[str, Any] | None = None
    note: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None

    @property
    def is_end_state(self) -> bool:
        """Whether this observation establishes the real end state rather than a rerun."""

        return self.verification_strength is VerificationStrength.INDEPENDENTLY_OBSERVED_END_STATE

    @property
    def is_read_only(self) -> bool:
        """Whether the probe that produced this observation mutated nothing."""

        return self.read_only
