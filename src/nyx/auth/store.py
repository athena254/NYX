"""The principal registry: configuration in, authenticated capabilities out.

The store is built from settings, never from request data. A configured principal's
raw key is read from the environment variable it names at load time; if that
variable is unset the principal is not loaded (fail closed) and a warning names
only the principal and the variable, never a key value.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass

from nyx.auth.apikey import hash_api_key, keys_match
from nyx.auth.principal import AuthenticatedPrincipal
from nyx.config import Settings
from nyx.domain.base import PrincipalRef
from nyx.domain.enums import PrincipalKind
from nyx.findings.lifecycle import Capability

_logger = logging.getLogger("nyx.auth")

# Reserved for the loopback local-operator identity created by ``anonymous()``.
ANONYMOUS_PRINCIPAL_ID = "local-operator"

# The deployment owner: every capability the lifecycle machine understands. The
# only caller that receives this without a credential is the loopback local
# operator in ``auth_mode='none'`` (ADR 0007).
_OWNER_CAPABILITIES = frozenset(Capability)


@dataclass(frozen=True)
class PrincipalRecord:
    """One configured principal. Only the key *hash* is held in memory."""

    principal_id: str
    key_hash: str
    capabilities: frozenset[Capability]
    description: str


class CapabilityStore:
    """Authenticate presented keys and answer capability questions by principal id."""

    def __init__(self, principals: Sequence[PrincipalRecord]) -> None:
        self._principals = tuple(principals)
        self._capabilities = {
            record.principal_id: record.capabilities for record in self._principals
        }

    def authenticate(self, presented_key: str) -> AuthenticatedPrincipal | None:
        """Return the principal whose key matches, or ``None`` for an unknown key."""

        if not presented_key:
            return None
        for record in self._principals:
            if keys_match(presented_key, record.key_hash):
                return AuthenticatedPrincipal(
                    id=PrincipalRef(
                        id=record.principal_id,
                        kind=PrincipalKind.PRINCIPAL,
                        display_name=record.description,
                    ),
                    capabilities=record.capabilities,
                    auth_mode="api_key",
                )
        return None

    def anonymous(self) -> AuthenticatedPrincipal:
        """Return the loopback local operator with the deployment-owner capabilities.

        This is only reachable in ``auth_mode='none'``/``local``; ``config.py``
        refuses a non-loopback bind in ``none`` mode (ADR 0007).
        """

        return AuthenticatedPrincipal(
            id=PrincipalRef(
                id=ANONYMOUS_PRINCIPAL_ID,
                kind=PrincipalKind.HUMAN,
                display_name="Local operator",
            ),
            capabilities=_OWNER_CAPABILITIES,
            auth_mode="none",
        )

    def capabilities_for(self, principal_id: str) -> frozenset[Capability]:
        """Return the capabilities held by ``principal_id`` (empty when unknown)."""

        if principal_id == ANONYMOUS_PRINCIPAL_ID:
            return _OWNER_CAPABILITIES
        return self._capabilities.get(principal_id, frozenset())

    @classmethod
    def load_from_settings(cls, settings: Settings) -> CapabilityStore:
        """Build a store from ``settings.auth_principals`` and ``settings.auth_api_key``.

        Capability strings are validated here rather than in ``config.py`` so the
        configuration module does not import the lifecycle enum. An unknown value
        raises ``ValueError`` naming the principal and the value.
        """

        records: list[PrincipalRecord] = []
        for entry in settings.auth_principals:
            principal_id = str(entry.get("principal_id", ""))
            if principal_id == ANONYMOUS_PRINCIPAL_ID:
                raise ValueError(
                    f"principal id '{principal_id}' is reserved for the loopback local operator"
                )
            env_name = str(entry.get("api_key_env", ""))
            raw_key = os.environ.get(env_name) if env_name else None
            if not raw_key:
                _logger.warning(
                    "auth principal %r not loaded: environment variable %s is unset",
                    principal_id,
                    env_name or "<missing>",
                )
                continue
            capabilities = _parse_capabilities(principal_id, entry.get("capabilities", []))
            records.append(
                PrincipalRecord(
                    principal_id=principal_id,
                    key_hash=hash_api_key(raw_key),
                    capabilities=capabilities,
                    description=str(
                        entry.get("description") or f"configured principal {principal_id}"
                    ),
                )
            )

        operator_key = settings.auth_api_key
        if operator_key is not None:
            raw_operator_key = operator_key.get_secret_value()
            if raw_operator_key:
                records.append(
                    PrincipalRecord(
                        principal_id="operator",
                        key_hash=hash_api_key(raw_operator_key),
                        capabilities=_OWNER_CAPABILITIES,
                        description="single-operator API key",
                    )
                )

        return cls(records)


def _parse_capabilities(principal_id: str, values: Sequence[str]) -> frozenset[Capability]:
    parsed: set[Capability] = set()
    for value in values:
        try:
            parsed.add(Capability(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"principal '{principal_id}' declares unknown capability '{value}'"
            ) from exc
    if not parsed:
        raise ValueError(f"principal '{principal_id}' declares no valid capabilities")
    return frozenset(parsed)
