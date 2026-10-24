"""The Ghost Factory demonstration seed (H5).

`nyx seed demo` loads the checked-in fixtures through the ordinary ingestion gateway, so the
demonstration inherits every property production traffic gets. See :mod:`nyx.seed.demo`.
"""

from __future__ import annotations

from nyx.seed.demo import (
    DEFAULT_FIXTURE_DIR,
    SEED_ORDER,
    SeedError,
    SeedOutcome,
    seed_demo,
    seed_demo_from_settings,
)

__all__ = [
    "DEFAULT_FIXTURE_DIR",
    "SEED_ORDER",
    "SeedError",
    "SeedOutcome",
    "seed_demo",
    "seed_demo_from_settings",
]
