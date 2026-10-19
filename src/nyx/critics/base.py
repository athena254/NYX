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

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import Field

from nyx.domain.base import Identifier, NyxModel, Scope
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    FindingCategory,
    Severity,
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
