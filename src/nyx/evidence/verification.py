"""Verification strength: one mapping, shared by every critic and every probe.

The strength of a piece of support must not be decided by whichever critic happens to read it.
A probe that re-runs a test is worth ``rerun_by_nyx``; a probe that reads the live end state is
worth ``independently_observed_end_state``; a record the claimant produced about itself is worth
``self_reported`` and no more. Centralising the mapping here is what makes "C-001's verified" and
"C-008's proportionate" the same statement.

Ordering lives on :class:`~nyx.domain.enums.VerificationStrength`; this module only answers
"is this at least that strong?".
"""

from __future__ import annotations

from collections.abc import Iterable

from nyx.domain.enums import VerificationStrength
from nyx.domain.evidence import EvidenceItem
from nyx.domain.observation import Observation

#: The strength a completion claim needs before C-001 accepts its support. A rerun by NYX is the
#: floor: the claimant's own report never counts, and an observation NYX took of the environment
#: also clears it.
COMPLETION_MINIMUM_STRENGTH = VerificationStrength.RERUN_BY_NYX

_ORDER: dict[VerificationStrength, int] = {
    strength: index for index, strength in enumerate(VerificationStrength)
}


def is_at_least(candidate: VerificationStrength, minimum: VerificationStrength) -> bool:
    """Return whether ``candidate`` is at least as strong as ``minimum``."""

    return _ORDER[candidate] >= _ORDER[minimum]


def strongest(supports: Iterable[VerificationStrength]) -> VerificationStrength:
    """Return the strongest strength in ``supports``, or ``NONE`` when there are none.

    An empty support set is ``NONE`` rather than an error: "nothing backs this" is a real state
    that a critic must be able to see, not a crash.
    """

    values = list(supports)
    return max(values, key=lambda item: _ORDER[item]) if values else VerificationStrength.NONE


def strength_of_evidence(evidence: EvidenceItem) -> VerificationStrength:
    """Return the strength of a stored evidence item.

    Evidence is what a *producer* reported, so it can never be stronger than
    ``self_reported`` -- even a signed attestation is a claim about the world made by someone,
    and the signature establishes authenticity, not truth (ADR 0005). This is the function that
    stops a critic from treating a linked CI record as verification of the current commit.
    """

    return VerificationStrength.SELF_REPORTED


def strength_of_observation(observation: Observation) -> VerificationStrength:
    """Return the strength of an observation NYX gathered itself.

    Unlike evidence, an observation carries its own strength, decided by the probe kind that
    produced it. This is a pass-through rather than a re-derivation so that a probe can record
    what it is worth without a second place needing to know how it works.
    """

    return observation.verification_strength


def evidence_supports(
    claim_id: str,
    *,
    evidence: Iterable[EvidenceItem],
    observations: Iterable[Observation],
) -> VerificationStrength:
    """Return the strongest support available for ``claim_id``.

    The per-claim link is respected on both sides: only evidence whose ``relevance_to_claim`` is
    this claim, and only observations gathered against it. An observation with no claim link is
    not support for any particular claim, however strong it is -- a probe run against a subject
    is not the same as a probe run *for this claim*.

    The result is the strongest strength found, which may be ``NONE``. Callers decide whether
    that is enough with :func:`is_at_least`; this function does not hide a weak result by
    clamping it, because "nothing backs this" is a finding in its own right.
    """

    strengths = [
        strength_of_evidence(item) for item in evidence if item.relevance_to_claim == claim_id
    ]
    strengths.extend(
        strength_of_observation(item)
        for item in observations
        if item.relevance_to_claim == claim_id
    )
    return strongest(strengths)
