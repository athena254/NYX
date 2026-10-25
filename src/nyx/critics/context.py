"""The read-only input bundle a critic receives (SPEC §7 evidence protocol).

A :class:`CriticContext` is a pure data transfer object. It contains the claim or event under
assessment, the records linked to it, the resolved scope, and a clock reading. Critics never
see a ``Session`` and never write to storage directly: they return a
:class:`~nyx.critics.base.CriticResult` and the service layer persists the run and composes any
finding. This keeps critics deterministic, testable, and free of side effects.

The context is assembled by :func:`build_context` from persisted records. It is the only place
where the shape of "what a critic can see" is defined.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from nyx.domain.base import NyxModel, Scope, UtcDatetime, utc_now
from nyx.domain.claims import Claim
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem
from nyx.domain.observation import Observation


class CriticContext(NyxModel):
    """Read-only view of everything one critic run may assess.

    ``target_event`` is the event that triggered the run. ``linked_claim`` is the claim the
    event refers to (via ``payload.claim_id`` or ``source_event_ids``), if any.
    ``related_claims`` are other stored claims about the same subject, which contradictory
    record critics need in order to compare statements. ``linked_evidence`` are the evidence
    items linked to the claim or the event. ``scope`` is the resolved scope of the assessment:
    the claim's scope when a claim is linked, otherwise the narrowest scope derivable from the
    event. ``now`` is the clock reading taken when the context was built, so a critic's
    freshness checks are reproducible.

    ``observations`` are what NYX itself gathered through a probe about the same subject. They
    are carried beside the evidence rather than merged into it, because a probe result and a
    producer's report are different kinds of thing: one is what NYX saw, the other is what
    someone said. A critic that needs to know whether a claim is *verified* rather than
    *reported* reads this list, and ``relevance_to_claim`` decides which observation belongs to
    which claim.
    """

    target_event: Event
    linked_claim: Claim | None = None
    related_claims: list[Claim] = Field(default_factory=list, max_length=64)
    linked_evidence: list[EvidenceItem] = Field(default_factory=list, max_length=64)
    observations: list[Observation] = Field(default_factory=list, max_length=64)
    scope: Scope
    now: UtcDatetime = Field(default_factory=utc_now)


def _event_scope(event: Event) -> Scope:
    """Derive the narrowest scope an event can provide.

    The event payload is untrusted producer data; only ``commit``, ``artifact``, and
    ``environment`` keys are read, and only to narrow the scope. A system id alone is not a
    scoped assessment, so an event with no usable payload fields yields a bare system scope
    that critics must treat as out of their declared scope.
    """

    payload: dict[str, Any] = event.payload
    return Scope(
        system_id=event.source_system_id,
        artifact=str(payload["artifact"]) if payload.get("artifact") else None,
        commit=str(payload["commit"]) if payload.get("commit") else None,
        environment=str(payload["environment"]) if payload.get("environment") else None,
    )


def build_context(
    *,
    target_event: Event,
    linked_claim: Claim | None = None,
    related_claims: list[Claim] | None = None,
    linked_evidence: list[EvidenceItem] | None = None,
    observations: list[Observation] | None = None,
    now: UtcDatetime | None = None,
) -> CriticContext:
    """Assemble the read-only context for one critic run.

    The resolved scope prefers the linked claim's scope (claims must be scoped to be
    assessable) and falls back to the event-derived scope.
    """

    resolved = linked_claim.scope if linked_claim is not None else _event_scope(target_event)
    return CriticContext(
        target_event=target_event,
        linked_claim=linked_claim,
        related_claims=list(related_claims or []),
        linked_evidence=list(linked_evidence or []),
        observations=list(observations or []),
        scope=resolved,
        now=now or utc_now(),
    )
