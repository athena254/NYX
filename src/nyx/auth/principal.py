"""The authenticated caller identity passed through the HTTP dependency graph."""

from __future__ import annotations

from nyx.config import AuthMode
from nyx.domain.base import NyxModel, PrincipalRef
from nyx.findings.lifecycle import Capability


class AuthenticatedPrincipal(NyxModel):
    """A caller whose identity and capabilities were established for this request.

    ``auth_mode`` records *how* the identity was established. In ``none`` mode the
    caller is the loopback local operator (see ADR 0007); in ``api_key`` mode the
    identity comes from a key that matched a configured principal.
    """

    id: PrincipalRef
    capabilities: frozenset[Capability]
    auth_mode: AuthMode
