"""The tombstone: a typed placeholder that replaces a payload without preserving it.

A tombstone must answer three questions for a reader who no longer has the content:

* **what** was removed -- the action (``payload_redacted``, ``hash_removed``);
* **who** removed it and **why** -- the actor and the reason, so the fact of the change is
  auditable even though the content is gone (``privacy-and-retention.md`` §5 step 5);
* **when** -- so a later reviewer can date the change.

The original content is not recoverable from the tombstone. The function therefore never
receives it for any purpose other than being *discarded*: it is accepted as an argument only so
the call site reads as a replacement and so a caller cannot accidentally believe the original is
retained somewhere in the returned value. The value is not hashed, not embedded, not echoed.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

from nyx.domain.base import UtcDatetime, utc_now

TOMBSTONE_PREFIX = "[TOMBSTONE]"

ActionName = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9]+(\.[a-z0-9][a-z0-9_-]*)+$",
    ),
]

ReasonText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]

ActorName = Annotated[str, StringConstraints(min_length=1, max_length=128)]

# The rendered tombstone is deliberately bounded: it is stored in place of a payload field that
# may itself be a ``String(2000)`` column.
_MAX_RENDERED = 2000


def tombstone_payload(original: str, *, action: str, actor: str, reason: str) -> str:
    """Return a typed placeholder for ``original``, recording who removed it and why.

    ``original`` is never reflected in the result -- not in whole, not as a prefix, and not as a
    digest -- so a reader of the tombstone cannot recover the content it replaced.
    """

    del original  # The original is discarded; the tombstone must not be a function of it.
    return render_tombstone(action=action, actor=actor, reason=reason)


def render_tombstone(*, action: str, actor: str, reason: str, at: UtcDatetime | None = None) -> str:
    """Render the tombstone text for a redaction that already happened.

    The audit writer stamps its own timestamp, so the caller supplies ``at`` only when the
    tombstone is being reconstructed for display rather than for storage.
    """

    moment = utc_now() if at is None else at
    body = (
        f"{TOMBSTONE_PREFIX} action={action}; actor={actor}; "
        f"reason={reason}; redacted_at={moment.isoformat().replace('+00:00', 'Z')}"
    )
    return body[:_MAX_RENDERED]


__all__ = [
    "TOMBSTONE_PREFIX",
    "ActionName",
    "ActorName",
    "ReasonText",
    "render_tombstone",
    "tombstone_payload",
]
