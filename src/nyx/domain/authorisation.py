"""The authorisation aggregate — who approved what, for what scope, until when.

An authorisation record is the fourth evidence kind. It answers the question C-006 asks
("was this risky action approved?") with a *record* rather than a reference: an approver identity
that must differ from the actor, a scope that must cover the action taken, and an expiry that must
not have passed.

It exists because a payload field named ``approval_ref`` proves only that a string was present.
It cannot express "approved for staging but not production", "approved for 48 hours", or "approved
by the same person who performed the action" — and all three are the cases where the reference
gives false comfort.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from nyx.domain.base import (
    ActionName,
    Identifier,
    NyxModel,
    PrincipalRef,
    SchemaVersion,
    UtcDatetime,
    utc_now,
)
from nyx.domain.enums import Environment, RiskAction
from nyx.domain.ids import new_authorisation_id


class Authorisation(NyxModel):
    """One recorded approval of one action, by one approver, for a bounded scope.

    ``approver`` must differ from ``actor``. A record that fails the check is still stored —
    the record is the evidence, and self-approval is a finding, not a reason to discard the
    proof. ``validate`` is therefore a query, not a constructor constraint.

    ``scope_matches`` compares the authorisation's declared scope against the action's. It is a
    method rather than a field because what "matches" depends on what the action declares: an
    authorisation for ``environment=staging`` does not cover a deploy to ``production``.
    """

    id: Identifier = Field(default_factory=new_authorisation_id)
    schema_version: SchemaVersion = "1.0"
    action: ActionName
    risk_action: RiskAction
    actor: PrincipalRef
    approver: PrincipalRef
    environment: Environment
    #: Free-form scope description the approval covers: a repository, a namespace, a table,
    #: a queue. Compared against the action's own scope by :meth:`scope_matches`.
    scope_ref: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    approved_at: UtcDatetime
    expires_at: UtcDatetime | None = None
    subject_system_id: Identifier | None = None
    note: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None
    #: Set when the record was created to satisfy a mutating probe's gate rather than by a
    #: producer. A probe authorisation is an operational fact, not a claim about the world.
    granted_by: PrincipalRef | None = None

    @model_validator(mode="after")
    def _must_name_both_parties(self) -> Authorisation:
        if self.actor.id == self.approver.id:
            raise ValueError(
                "an authorisation must name an approver distinct from the actor; a record that "
                "cannot satisfy this is not an authorisation, and self-approval is a finding "
                "rather than a reason to store a malformed record"
            )
        return self

    def is_expired(self, at: UtcDatetime | None = None) -> bool:
        """Return whether this authorisation had lapsed by ``at`` (default: now)."""

        if self.expires_at is None:
            return False
        return self.expires_at < (at or utc_now())

    def scope_matches(self, action_scope: str) -> bool:
        """Return whether this authorisation's scope covers ``action_scope``.

        A single flat string comparison is deliberately chosen over a scope grammar: the
        authorisation records what a human wrote, and inventing a parser for it would make the
        match a judgement rather than a check. Documented in ``docs/probe-authoring.md``.
        """

        return _normalise(self.scope_ref) == _normalise(action_scope)


def _normalise(value: str) -> str:
    """Lowercase, strip, and collapse whitespace so a stray space cannot fake a mismatch."""

    return " ".join(value.lower().split())
