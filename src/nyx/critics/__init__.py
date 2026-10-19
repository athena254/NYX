"""The deterministic critic pack (SPEC §8).

Importing this package registers the built-in critics. Registration is explicit — there is no
magic discovery — so the set of critics that can run is always visible in this file.

One critic is the exception. C-010 (NYX self-consistency) inspects NYX's *own* store, so it
needs a ``SessionFactory``. It is registered by :func:`register_storage_backed`, which the API
factory and the CLI both call with the session factory they already built. Registering it with
an independently-constructed factory would let it audit a different database than the one in
use — worse than not running it at all. Until it is registered C-010 is simply absent rather
than present-and-broken, and if it is constructed without a factory it returns an ERROR result,
never a silent pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nyx.critics.base import Critic, CriticResult, CriticScope
from nyx.critics.c001_completion_vs_verification import CompletionVsVerificationCritic
from nyx.critics.c002_contradictory_records import ContradictoryRecordsCritic
from nyx.critics.c003_evidence_freshness import EvidenceFreshnessCritic
from nyx.critics.c004_provenance_gap import ProvenanceGapCritic
from nyx.critics.c005_policy_violation import PolicyViolationCritic
from nyx.critics.c006_risky_action_without_authorisation import (
    RiskyActionWithoutAuthorisationCritic,
)
from nyx.critics.c007_repeated_failure_regression import RepeatedFailureRegressionCritic
from nyx.critics.c008_unsupported_certainty import UnsupportedCertaintyCritic
from nyx.critics.c009_evidence_conflict import EvidenceConflictCritic
from nyx.critics.c010_nyx_self_consistency import NyxSelfConsistencyCritic
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

if TYPE_CHECKING:
    from nyx.storage.db import SessionFactory

register(CompletionVsVerificationCritic())
register(ContradictoryRecordsCritic())
register(EvidenceFreshnessCritic())
register(ProvenanceGapCritic())
register(PolicyViolationCritic())
register(RiskyActionWithoutAuthorisationCritic())
register(RepeatedFailureRegressionCritic())
register(UnsupportedCertaintyCritic())
register(EvidenceConflictCritic())


def register_storage_backed(session_factory: SessionFactory) -> None:
    """Register the critics that inspect NYX's own store.

    The caller passes the session factory it already built, so the critic reads exactly the
    store that caller writes to. A second, independently-constructed factory would let the
    self-consistency critic audit a different database than the one in use.
    """

    register(NyxSelfConsistencyCritic(session_factory))


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
    "register_storage_backed",
    "run_selected",
]
