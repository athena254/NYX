"""API-key hashing and comparison.

The raw key is never persisted or logged. Configuration stores only the SHA-256
hex digest, and comparison is constant-time over the digest so an observer cannot
learn a valid key one byte at a time from response timing.
"""

from __future__ import annotations

import hashlib
import hmac


def hash_api_key(key: str) -> str:
    """Return the SHA-256 hex digest of ``key``."""

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def keys_match(presented: str, expected_hash: str) -> bool:
    """Return whether ``presented`` hashes to ``expected_hash``, in constant time."""

    return hmac.compare_digest(hash_api_key(presented), expected_hash)
