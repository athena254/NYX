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
from nyx.critics.c012_scope_creep import ScopeCreepCritic
from nyx.critics.c016_claimed_vs_observed_side_effects import (
    ClaimedVsObservedSideEffectsCritic,
)
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
    from nyx.config import Settings
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
register(ScopeCreepCritic())
register(ClaimedVsObservedSideEffectsCritic())


def register_storage_backed(session_factory: SessionFactory) -> None:
    """Register the critics that inspect NYX's own store.

    The caller passes the session factory it already built, so the critic reads exactly the
    store that caller writes to. A second, independently-constructed factory would let the
    self-consistency critic audit a different database than the one in use.
    """

    register(NyxSelfConsistencyCritic(session_factory))


def register_ground_truth(settings: Settings) -> bool:
    """Switch the critics with a stronger ground-truth rule on, when their flag is set.

    Re-registration replaces the pack's instance, so this must run **before** any critic runs in
    the process. It returns whether any stronger rule is active, so the caller can log the state
    rather than discover it from behaviour.

    With every flag off this is a no-op and each critic keeps its pre-ground-truth behaviour,
    which is what makes "flags off equals current behaviour" testable rather than aspirational.
    Each flag is independent: a deployment can adopt C-001's rule without adopting C-006's.

    The authorisation lookup for C-006 is deliberately *not* wired here. It needs a store, and
    this module must not open one: a registration function that constructs a session is a
    function that can outlive the request that built it. C-006's stronger rule therefore runs
    only where a caller that owns a session passes the lookup in explicitly.
    """

    active = False

    if settings.require_independent_verification:
        register(CompletionVsVerificationCritic(require_strength=True))
        active = True
    if settings.require_coverage_scope:
        register(EvidenceFreshnessCritic(require_coverage_scope=True))
        active = True
    if settings.require_verifiable_origin:
        register(ProvenanceGapCritic(require_verifiable_origin=True))
        active = True
    if settings.require_valid_authorisation:
        register(RiskyActionWithoutAuthorisationCritic(require_valid_authorisation=True))
        active = True
    return active


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
    "register_ground_truth",
    "register_storage_backed",
    "run_selected",
]
