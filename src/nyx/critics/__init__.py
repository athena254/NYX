"""The deterministic critic pack (SPEC §8).

Importing this package registers the built-in critics. Registration is explicit — there is
no magic discovery — so the set of critics that can run is always visible in this file.
"""

from __future__ import annotations

from nyx.critics.base import Critic, CriticResult, CriticScope
from nyx.critics.c001_completion_vs_verification import CompletionVsVerificationCritic
from nyx.critics.c002_contradictory_records import ContradictoryRecordsCritic
from nyx.critics.c003_evidence_freshness import EvidenceFreshnessCritic
from nyx.critics.c004_provenance_gap import ProvenanceGapCritic
from nyx.critics.context import CriticContext
from nyx.critics.registry import (
    CriticInfo,
    CriticRegistryError,
    CriticRunOutcome,
    get,
    list_critics,
    register,
    run_selected,
)

register(CompletionVsVerificationCritic())
register(ContradictoryRecordsCritic())
register(EvidenceFreshnessCritic())
register(ProvenanceGapCritic())

__all__ = [
    "Critic",
    "CriticContext",
    "CriticInfo",
    "CriticRegistryError",
    "CriticResult",
    "CriticRunOutcome",
    "CriticScope",
    "get",
    "list_critics",
    "register",
    "run_selected",
]
