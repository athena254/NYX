"""Recorded policy versions, and who changed them (acceptance scenario 22).

``PolicyRegistry`` in :mod:`nyx.policies.engine` is in-memory and holds *current* rules. It records
nothing about a change: no actor, no timestamp, no reason. This module is that missing half -- the
append-only record of every policy change, in the ``policy_versions`` table.

The two halves are deliberately joined in one call. ``PolicyVersionStore.register`` makes the rule
live in the registry *and* records who changed it, so a policy can never become active without a
recorded change. That coupling is what scenario 22 actually asks for.

Design note on re-recording
---------------------------
Registering a version that is already recorded is refused rather than replaced. History is not
editable: to change a rule you register a new version. ``PolicyRegistry.add`` would happily replace
an in-memory entry with the same ``(rule_id, version)``, which would make the registry and the
append-only record disagree -- so the refusal happens first and the database unique constraint on
``(rule_id, version)`` remains as the backstop against a race.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from nyx.disputes.models import PolicyVersion
from nyx.domain.base import Identifier, utc_now
from nyx.error_codes import ErrorCode
from nyx.policies.engine import PolicyRegistry, Rule
from nyx.storage.repositories.policy_versions import PolicyVersionRepository


class PolicyChangeError(Exception):
    """A policy change could not be recorded.

    Carries a stable ``code`` from the shared catalogue so the API and CLI can map the failure
    without importing this module's internals.
    """

    code: ErrorCode

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.detail = message


class MissingPolicyActorError(PolicyChangeError):
    """A policy change was attempted without recording who made it."""

    code = ErrorCode.INVALID_REQUEST


class DuplicatePolicyVersionError(PolicyChangeError):
    """The policy version being registered is already recorded."""

    code = ErrorCode.CONFLICT


class PolicyVersionStore:
    """Append-only record of policy changes. Never commits implicitly."""

    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = PolicyVersionRepository(session)
        self._clock = clock

    def register(
        self,
        registry: PolicyRegistry,
        rule: Rule,
        changed_by: Identifier,
        change_reason: str,
    ) -> PolicyVersion:
        """Make ``rule`` live in ``registry`` and record the change.

        Both effects happen together on purpose: a rule must not become active without a recorded
        actor and reason. The returned :class:`PolicyVersion` carries the content digest of the rule
        revision that was activated.

        Raises :class:`MissingPolicyActorError` when ``changed_by`` is empty (an actor is never
        defaulted), and :class:`DuplicatePolicyVersionError` when the version is already recorded.
        """

        if not changed_by:
            raise MissingPolicyActorError(
                "a policy change must record who made it; changed_by is required"
            )
        if not change_reason.strip():
            raise PolicyChangeError(
                "a policy change must record why it was made; change_reason is required"
            )
        existing = self._repository.latest(rule.rule_id)
        if existing is not None and existing.version == rule.version:
            raise DuplicatePolicyVersionError(
                f"policy rule {rule.rule_id!r} version {rule.version!r} is already recorded; "
                "register a new version to change the rule"
            )

        # Activate first, then record: the digest is taken from the registry entry that is now in
        # force, so the recorded change and the live rule cannot describe different revisions.
        registry.add(rule)
        version = PolicyVersion(
            rule_id=rule.rule_id,
            version=rule.version,
            digest=registry.digest(rule.rule_id, rule.version),
            changed_by=changed_by,
            changed_at=self._clock(),
            change_reason=change_reason,
        )
        self._repository.add(version)
        return version

    def latest(self, rule_id: str) -> PolicyVersion | None:
        """Return the newest recorded version of ``rule_id``, or ``None`` if never changed."""

        return self._repository.latest(rule_id)

    def version_in_force(self, rule_id: str) -> PolicyVersion | None:
        """Alias for :meth:`latest`, named for the read a disposition needs.

        A disposition records which policy version decided it, so the question "which version was in
        force" is the one this store answers most often.
        """

        return self._repository.latest(rule_id)

    def list(
        self,
        *,
        rule_id: str | None = None,
        changed_by: str | None = None,
    ) -> Sequence[PolicyVersion]:
        """Return recorded policy changes, newest first for each rule."""

        return self._repository.list(rule_id=rule_id, changed_by=changed_by)
