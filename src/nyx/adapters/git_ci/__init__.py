"""The Git/CI adapter's public surface (phase 5, slice 5.1).

Only the bounded, read-only HTTP client and its configuration are exported. Normalization and
adapter assembly arrive in later slices (5.2, 5.3) and import from here.
"""

from __future__ import annotations

from nyx.adapters.git_ci.config import GitCiConfig
from nyx.adapters.git_ci.github_client import GitCiError, GitHubCiClient

__all__ = [
    "GitCiConfig",
    "GitCiError",
    "GitHubCiClient",
]
