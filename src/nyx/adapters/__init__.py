"""Adapters: read-only observers behind the ingestion boundary (ADR 0008).

Importing this package must stay cheap and must not pull the deterministic core in. Gate
G7 (``tests/contract/test_adapter_boundary.py``) asserts that no module under
``src/nyx/adapters/`` imports from ``nyx.critics``, ``nyx.findings``, ``nyx.disputes``,
``nyx.policies``, ``nyx.storage``, ``nyx.retention``, ``nyx.auth``, ``nyx.systems``, or
``nyx.audit``; and that no module outside this package imports ``nyx.adapters`` except
``api/app.py`` and ``cli/main.py``.
"""

from nyx.adapters.base import (
    Adapter,
    AdapterDescriptor,
    AdapterHealth,
    AdapterInput,
    AdapterRegistry,
    AdapterState,
    UnknownAdapterError,
)

__all__ = [
    "Adapter",
    "AdapterDescriptor",
    "AdapterHealth",
    "AdapterInput",
    "AdapterRegistry",
    "AdapterState",
    "UnknownAdapterError",
]
