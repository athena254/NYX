"""The monitored-system aggregate (SPEC §5.1)."""

from __future__ import annotations

from typing import Annotated, Any, Self

from pydantic import Field, StringConstraints, model_validator

from nyx.domain.base import (
    Identifier,
    NyxModel,
    SchemaVersion,
    UtcDatetime,
    secretish_key_path,
    utc_now,
)
from nyx.domain.enums import AdapterKind, Environment, SystemKind, SystemStatus
from nyx.domain.ids import new_system_id

DottedCapability = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$",
    ),
]


class MonitoredSystem(NyxModel):
    """A system registered for observation.

    ``metadata`` is descriptive only. Credentials, tokens, private keys, and raw secrets are
    forbidden there; the validator rejects any key that looks like secret material. This is
    safety invariant 8 at the domain boundary.
    """

    id: Identifier = Field(default_factory=new_system_id)
    schema_version: SchemaVersion = "1.0"
    name: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=128,
            pattern=r"^[a-z0-9][a-z0-9._-]*$",
        ),
    ]
    kind: SystemKind
    environment: Environment
    owner: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    capabilities: list[DottedCapability] = Field(default_factory=list, max_length=64)
    adapter: AdapterKind
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=64)
    created_at: UtcDatetime = Field(default_factory=utc_now)
    status: SystemStatus = SystemStatus.ACTIVE

    @model_validator(mode="after")
    def _reject_secret_metadata(self) -> Self:
        path = secretish_key_path(self.metadata)
        if path is not None:
            raise ValueError(
                f"metadata key {path!r} looks like secret material; "
                "credentials and secrets must not be stored in metadata"
            )
        return self
