"""Shared domain primitives: UTC timestamps, identifiers, and value objects.

All timestamps in NYX are timezone-aware UTC. Naive datetimes are rejected rather than
silently interpreted as local time, and JSON serialisation renders the canonical trailing
``Z`` form required by the Phase 0 schemas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
)

from nyx.domain.enums import PrincipalKind

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]

SchemaVersion = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]+\.[0-9]+$"),
]

Digest = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]

ActionName = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9]+(\.[a-z0-9][a-z0-9_-]*)+$",
    ),
]


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""

    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Normalise an aware datetime to UTC, rejecting naive datetimes."""

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware; naive datetimes are not allowed")
    return value.astimezone(UTC)


def format_utc(value: datetime) -> str:
    """Render a datetime as ISO-8601 UTC with a trailing ``Z``."""

    return ensure_utc(value).isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[
    datetime,
    AfterValidator(ensure_utc),
    PlainSerializer(format_utc, return_type=str, when_used="json"),
]


class NyxModel(BaseModel):
    """Base model with the strictness conventions used across the domain."""

    model_config = ConfigDict(extra="forbid")


class PrincipalRef(NyxModel):
    """A reference to an actor, subject, or producer.

    The identity is self-asserted unless a separate trusted channel establishes it.
    """

    id: Identifier
    kind: PrincipalKind
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None


class Period(NyxModel):
    """The time window a claim or evidence item applies to."""

    from_: UtcDatetime = Field(alias="from")
    to: UtcDatetime | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Scope(NyxModel):
    """The system, artifact, environment, period, and conditions a statement applies to.

    ``covered`` names the files, modules, or endpoints the statement *covers*. Evidence
    declares what its verification actually exercised; a claim declares what the claimed change
    touched. Both are optional and both are unread unless set: a scope that declares neither is
    not accused of anything by the freshness critic, which can only compare two declared sets.
    ``covered`` deliberately does not participate in :meth:`has_detail` -- a scope that says only
    which files it covers, and not which artifact or version, is not a narrowed scope.
    """

    system_id: Identifier
    artifact: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    commit: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    environment: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    period: Period | None = None
    conditions: list[Annotated[str, StringConstraints(min_length=1, max_length=256)]] = Field(
        default_factory=list, max_length=32
    )
    covered: list[Annotated[str, StringConstraints(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=64
    )

    def has_detail(self) -> bool:
        """Return whether the scope narrows beyond the bare system identity."""

        return any(
            (
                self.artifact is not None,
                self.commit is not None,
                self.environment is not None,
                self.period is not None,
                bool(self.conditions),
            )
        )


def secretish_key_path(value: Any, path: str = "metadata") -> str | None:
    """Return the path of the first key that looks like a secret, if any.

    Matching is case-insensitive and tolerant of ``-`` versus ``_`` separators. Nested
    dictionaries are inspected because secret material is often placed one level down.
    """

    markers = (
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "credential",
        "private_key",
    )
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).lower().replace("-", "_").replace(" ", "_")
            collapsed = key.replace("_", "")
            if any(marker in key for marker in markers) or any(
                marker.replace("_", "") in collapsed for marker in markers
            ):
                return f"{path}.{raw_key}"
            nested = secretish_key_path(child, f"{path}.{raw_key}")
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for index, child in enumerate(value):
            nested = secretish_key_path(child, f"{path}[{index}]")
            if nested is not None:
                return nested
    return None
