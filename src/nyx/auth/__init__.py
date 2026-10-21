"""Authentication and capability-based authorisation (Phase 4).

The lifecycle machine asks a :class:`~nyx.findings.lifecycle.CapabilityChecker` whether a
principal holds a capability; this package is where that question gets a real answer. Until
Phase 4 the application bound ``DenyAllCapabilityChecker``, so no principal could change a
disposition.

Placement: this is core NYX, not a separate project. It is wired in ``api/app.py`` and consumed
by ``api/deps.py`` and the evidence route.
"""
