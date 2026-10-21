"""Request authentication and capability dependencies.

The trust decision (ADR 0007): ``auth_mode='none'`` is reachable only on a loopback
bind, so a loopback request without a credential is the local operator. Every other
mode requires a credential on every request; a missing or unknown credential is
``401 UNAUTHENTICATED`` and never falls back to the operator identity.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request

from nyx.api.errors import ApiError
from nyx.auth.principal import AuthenticatedPrincipal
from nyx.auth.store import CapabilityStore
from nyx.config import Settings
from nyx.error_codes import ErrorCode
from nyx.findings.lifecycle import Capability

_EVIDENCE_CAPABILITIES = frozenset({Capability.EVIDENCE_READ, Capability.EVIDENCE_RESTRICTED})


def get_capability_store(request: Request) -> CapabilityStore:
    """Return the application's capability store, building it from settings once."""

    store = getattr(request.app.state, "capability_store", None)
    if isinstance(store, CapabilityStore):
        return store
    store = CapabilityStore.load_from_settings(_settings_for(request))
    request.app.state.capability_store = store
    return store


def current_principal(request: Request) -> AuthenticatedPrincipal:
    """Return the authenticated caller for this request, or raise ``401``.

    In local-only modes there is no credential: the caller is the local operator.
    In ``api_key`` mode a credential is mandatory and an unknown key never
    degrades to the operator identity.
    """

    settings = _settings_for(request)
    store = get_capability_store(request)
    if settings.auth_mode in ("none", "local"):
        return store.anonymous()

    presented = _presented_key(request)
    if presented is None:
        raise ApiError(
            ErrorCode.UNAUTHENTICATED,
            message="No API credential was presented.",
        )
    principal = store.authenticate(presented)
    if principal is None:
        raise ApiError(
            ErrorCode.UNAUTHENTICATED,
            message="The presented API credential is not valid.",
        )
    return principal


def require_capability(capability: Capability) -> Callable[..., AuthenticatedPrincipal]:
    """Build a dependency that requires ``capability`` on the current principal.

    The authenticated principal carries the capabilities the store resolved, so a
    caller cannot claim a capability through a request header or payload.
    """

    def dependency(
        principal: AuthenticatedPrincipal = Depends(current_principal),
    ) -> AuthenticatedPrincipal:
        if capability not in principal.capabilities:
            code = (
                ErrorCode.EVIDENCE_ACCESS_DENIED
                if capability in _EVIDENCE_CAPABILITIES
                else ErrorCode.FORBIDDEN
            )
            raise ApiError(
                code,
                message=f"Capability '{capability.value}' is required for this action.",
            )
        return principal

    return dependency


def _settings_for(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _presented_key(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    api_key = request.headers.get("x-api-key")
    if api_key and api_key.strip():
        return api_key.strip()
    return None
