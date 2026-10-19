"""Stable, prefixed, sortable identifiers for NYX records.

Choice
------
IDs use a prefix plus a ULID-like 26-character Crockford-Base32 token. The first 48 bits of
the token are a millisecond Unix timestamp and the remaining 80 bits are cryptographically
random. This gives lexicographic ordering by creation time (and therefore deterministic
pagination) without depending on Python 3.14's ``uuid.uuid7``, since NYX still supports
Python 3.12.

Generation is monotonic within a process: if two IDs are requested in the same millisecond,
the random component is incremented so that the second token still sorts after the first.
The random component never wraps into an earlier timestamp. IDs are opaque; callers must not
parse them to recover time beyond coarse ordering.

IDs are stable once assigned and are never reused. Deletion or redaction does not liberate an
identifier for another record.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Literal

IdPrefix = Literal[
    "sys_",
    "evt_",
    "clm_",
    "ev_",
    "fnd_",
    "crn_",
    "rbt_",
    "dsp_",
    "aud_",
]

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TOKEN_LENGTH = 26
_RANDOM_BITS = 80
_RANDOM_MASK = (1 << _RANDOM_BITS) - 1

_lock = threading.Lock()
_last_millisecond = -1
_last_random = 0


def _encode(value: int, length: int) -> str:
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _CROCKFORD[value & 0x1F]
        value >>= 5
    return "".join(chars)


def _next_token() -> str:
    global _last_millisecond, _last_random

    with _lock:
        millisecond = time.time_ns() // 1_000_000
        if millisecond <= _last_millisecond:
            millisecond = _last_millisecond
            candidate = (_last_random + 1) & _RANDOM_MASK
            if candidate == 0:
                millisecond += 1
                candidate = secrets.randbits(_RANDOM_BITS)
        else:
            candidate = secrets.randbits(_RANDOM_BITS)
        _last_millisecond = millisecond
        _last_random = candidate
        value = (millisecond << _RANDOM_BITS) | candidate
        return _encode(value, _TOKEN_LENGTH)


def new_id(prefix: IdPrefix) -> str:
    """Return a new identifier with the supplied record prefix."""

    return f"{prefix}{_next_token()}"


def new_request_id() -> str:
    """Return a new server-generated request/correlation identifier."""

    return f"req_{_next_token()}"


def new_system_id() -> str:
    return new_id("sys_")


def new_event_id() -> str:
    return new_id("evt_")


def new_claim_id() -> str:
    return new_id("clm_")


def new_evidence_id() -> str:
    return new_id("ev_")


def new_finding_id() -> str:
    return new_id("fnd_")


def new_critic_run_id() -> str:
    return new_id("crn_")


def new_rebuttal_id() -> str:
    return new_id("rbt_")


def new_disposition_id() -> str:
    return new_id("dsp_")


def new_audit_id() -> str:
    return new_id("aud_")
