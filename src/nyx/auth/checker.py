"""The real :class:`~nyx.findings.lifecycle.CapabilityChecker` implementation."""

from __future__ import annotations

from nyx.auth.store import CapabilityStore
from nyx.domain.base import PrincipalRef
from nyx.findings.lifecycle import Capability


class ConfigBackedCapabilityChecker:
    """Answer capability questions from the configured principal store."""

    def __init__(self, store: CapabilityStore) -> None:
        self._store = store

    def has_capability(self, principal: PrincipalRef, capability: Capability) -> bool:
        """Return whether ``principal`` holds ``capability``."""

        return capability in self._store.capabilities_for(principal.id)
