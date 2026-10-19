"""Domain invariant and validation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimStatus,
    ClaimType,
    Environment,
    EvidenceKind,
    IntegrityMethod,
    Sensitivity,
    SystemKind,
    TrustClass,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem
from nyx.domain.ids import new_system_id
from nyx.domain.systems import MonitoredSystem

_DIGEST = "sha256:" + "0" * 64


def _system(metadata: dict[str, object] | None = None) -> MonitoredSystem:
    return MonitoredSystem(
        name="ghost-factory",
        kind=SystemKind.REPOSITORY,
        environment=Environment.STAGING,
        owner="demo-team",
        capabilities=["events.write", "findings.read"],
        adapter="http",
        metadata=metadata or {},
    )


def _event(**overrides: object) -> Event:
    values: dict[str, object] = {
        "source_system_id": new_system_id(),
        "event_type": "completion.claim.submitted",
        "occurred_at": datetime(2026, 10, 19, 10, 0, tzinfo=UTC),
        "received_at": datetime(2026, 10, 19, 10, 0, 1, tzinfo=UTC),
        "correlation_id": "run-1",
        "payload": {"statement": "done"},
        "content_hash": _DIGEST,
        "trust_class": TrustClass.OBSERVED,
        "retention_class": "standard",
    }
    values.update(overrides)
    return Event.model_validate(values)


def _claim(scope: dict[str, object]) -> Claim:
    return Claim.model_validate(
        {
            "statement": "All required tests pass.",
            "claimant_system_id": new_system_id(),
            "claim_type": ClaimType.COMPLETION,
            "scope": scope,
        }
    )


def _evidence(**overrides: object) -> EvidenceItem:
    values: dict[str, object] = {
        "kind": EvidenceKind.CI_TEST_RESULT,
        "title": "CI run",
        "description": "A CI test result for one commit.",
        "source_system_id": new_system_id(),
        "source_ref": "https://ci.example.invalid/runs/1",
        "captured_at": datetime(2026, 10, 19, 9, 55, tzinfo=UTC),
        "valid_from": datetime(2026, 10, 19, 9, 55, tzinfo=UTC),
        "integrity_method": IntegrityMethod.HASH,
        "relevance_to_claim": "clm_example",
        "limitations": ["The run does not cover untested paths."],
        "sensitivity": Sensitivity.INTERNAL,
        "access_policy_ref": "policy:evidence-internal-read",
        "retention_class": "standard",
    }
    values.update(overrides)
    return EvidenceItem.model_validate(values)


def test_naive_datetimes_are_rejected() -> None:
    naive = datetime(2026, 10, 19, 10, 0, 0)

    with pytest.raises(ValidationError):
        _event(occurred_at=naive)

    with pytest.raises(ValidationError):
        MonitoredSystem(
            name="naive-system",
            kind=SystemKind.SERVICE,
            environment=Environment.LOCAL,
            owner="team",
            adapter="cli",
            created_at=naive,
        )

    with pytest.raises(ValidationError):
        _claim(
            {
                "system_id": new_system_id(),
                "commit": "abc123",
                "period": {"from": naive},
            }
        )


def test_aware_datetimes_are_normalised_to_utc_z() -> None:
    plus_two = timezone(timedelta(hours=2))
    event = _event(
        occurred_at=datetime(2026, 10, 19, 12, 0, tzinfo=plus_two),
        received_at=datetime(2026, 10, 19, 12, 0, 1, tzinfo=plus_two),
    )

    assert event.occurred_at == datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    assert event.model_dump(mode="json")["occurred_at"] == "2026-10-19T10:00:00Z"


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "client_secret",
        "access_token",
        "api_key",
        "apikey",
        "credentials",
        "private-key",
        "nested",
    ],
)
def test_secretish_metadata_keys_are_rejected(key: str) -> None:
    metadata: dict[str, object]
    if key == "nested":
        metadata = {"repository": "example/repo", "auth": {"client_token": "value"}}
    else:
        metadata = {key: "not-even-a-real-secret"}

    with pytest.raises(ValidationError, match="secret material"):
        _system(metadata=metadata)


def test_ordinary_metadata_is_accepted() -> None:
    system = _system(metadata={"repository": "example/repo", "default_branch": "main"})

    assert system.metadata["default_branch"] == "main"


def test_claim_scope_must_narrow_beyond_system_id() -> None:
    with pytest.raises(ValidationError):
        _claim({})

    with pytest.raises(ValidationError, match="claim scope must identify"):
        _claim({"system_id": new_system_id()})


def test_scoped_claim_is_accepted() -> None:
    claim = _claim(
        {
            "system_id": new_system_id(),
            "commit": "abc123",
            "environment": "staging",
        }
    )

    assert claim.status is ClaimStatus.UNASSESSED
    assert claim.scope.commit == "abc123"


def test_enum_values_are_enforced() -> None:
    with pytest.raises(ValidationError):
        _event(trust_class="trusted")

    with pytest.raises(ValidationError):
        Claim.model_validate(
            {
                "statement": "A claim",
                "claimant_system_id": new_system_id(),
                "claim_type": ClaimType.FACTUAL,
                "scope": {"system_id": new_system_id(), "commit": "abc123"},
                "status": "maybe",
            }
        )

    with pytest.raises(ValidationError):
        _evidence(sensitivity="secret")


def test_ids_are_prefixed_and_sortable() -> None:
    ids = [new_system_id() for _ in range(50)]

    assert all(identifier.startswith("sys_") for identifier in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
