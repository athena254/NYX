"""The critic contract (SPEC §7.2, §8).

A critic is a deterministic (in this phase) evaluator with a **stable identity** and an
**explicit, declared scope**. A ``no_finding`` result means only that this critic found no
issue *within its declared scope*; it is never a statement that the underlying claim is true
or that no other critic could find something.

This module deliberately contains no storage access and no orchestration. A critic receives a
read-only :class:`~nyx.critics.context.CriticContext` and returns a :class:`CriticResult`; the
service layer persists the run and composes any finding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Protocol, runtime_checkable

from pydantic import Field, StringConstraints

from nyx.domain.base import Digest, Identifier, NyxModel, Scope
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    FindingCategory,
    Reproducibility,
    Severity,
    VerificationStrength,
)
from nyx.findings.models import Confidence

if TYPE_CHECKING:
    from nyx.critics.context import CriticContext


class CriticScope(NyxModel):
    """What a critic can and cannot assess.

    The declared scope is part of the contract: it lets a reader interpret a ``no_finding``
    result correctly and lets the registry list critics honestly.
    """

    can_assess: list[str] = Field(default_factory=list, max_length=64)
    cannot_assess: list[str] = Field(default_factory=list, max_length=64)


class CriticResult(NyxModel):
    """The typed result a critic returns (SPEC §7.2).

    The first thirteen fields are the SPEC §7.2 contract. The remaining fields are additive
    composition hints: they carry the category, severity, and separated reasoning that the
    finding composer needs. They are optional so that a minimal §7.2 result remains valid.
    A critic must reference **persisted** evidence ids and must never invent them.
    """

    critic_id: Identifier
    critic_version: str = Field(min_length=1, max_length=64)
    result: CriticResultStatus
    proposition: str = Field(min_length=1, max_length=2000)
    scope: Scope
    observations: list[str] = Field(default_factory=list, max_length=64)
    supporting_evidence_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    counterevidence_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    limitations: list[str] = Field(default_factory=list, max_length=32)
    rationale: str = Field(min_length=1, max_length=2000)
    recommended_next_step: str = Field(min_length=1, max_length=2000)
    requires_human_review: bool = False

    category: FindingCategory | None = None
    severity: Severity | None = None
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=4000)
    inferences: list[str] = Field(default_factory=list, max_length=64)
    premises: list[str] = Field(default_factory=list, max_length=64)
    contradiction: str | None = Field(default=None, max_length=2000)
    recommendations: list[str] = Field(default_factory=list, max_length=32)
    unknowns: list[str] = Field(default_factory=list, max_length=32)
    affected_system_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    claim_ids: list[Identifier] = Field(default_factory=list, max_length=64)
    event_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    confidence: Confidence | None = None

    # Ground-truth extensions. All additive and all optional, so a critic that predates them
    # remains valid. They exist so that a finding can say *what it rests on* and *how strongly*
    # rather than only what it concluded:
    #
    # - observation_refs are the observations NYX gathered itself. They are kept out of
    #   supporting_evidence_ids on purpose: a probe result and a producer's report are different
    #   kinds of thing, and collapsing them is what lets a claim look verified when it is only
    #   reported.
    # - root_cause_key is the shared defect identity C-002 and C-009 key on, so one underlying
    #   conflict cannot raise two findings.
    # - subject_version is the commit/build/config hash the finding is about, so a finding about
    #   an old version is visibly about an old version.
    # - verification_strength is the strongest support behind the finding, on the shared enum.
    # - reproducibility records whether it replays.
    # - remediation_hint is advice, separate from the recommendation, so it can be short.
    observation_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    root_cause_key: Digest | None = None
    subject_version: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    reproducibility: Reproducibility | None = None
    verification_strength: VerificationStrength | None = None
    remediation_hint: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None

    # SPEC §13 provenance. A deterministic critic leaves these null; a model critic records the
    # provider, model, and prompt-template version that produced the assessment, plus token usage
    # and (only when logging is explicitly enabled) the redacted prompt/response pair.
    model_provider: str | None = Field(default=None, max_length=64)
    model_version: str | None = Field(default=None, max_length=128)
    prompt_version: str | None = Field(default=None, max_length=64)
    resource_usage: dict[str, float] | None = None
    llm_io: dict[str, Any] | None = None


@runtime_checkable
class Critic(Protocol):
    """The structural contract every critic implements."""

    critic_id: str
    critic_version: str
    description: str
    scope: CriticScope
    critic_type: CriticType

    def run(self, context: CriticContext) -> CriticResult:
        """Evaluate one read-only context and return a typed result."""
        ...
