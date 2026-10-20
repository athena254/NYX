"""Hand-written JSON Schemas and Pydantic domain models must agree on the same records.

Scope: every record type that has both a checked-in schema and a Pydantic model — system,
event, claim, evidence, audit, rebuttal, and disposition. ``finding`` has a model (Phase 2)
but no checked-in example, so it still cannot be compared here. The schemas each state that
this agreement test is what turns a Phase 0 contract into a verified artifact.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from nyx.disputes.models import Disposition, Rebuttal
from nyx.domain.audit import AuditEntry
from nyx.domain.claims import Claim
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem
from nyx.domain.systems import MonitoredSystem

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPOSITORY_ROOT / "schemas"
EXAMPLES = REPOSITORY_ROOT / "examples"

_AUDIT_EXAMPLE: dict[str, Any] = {
    "id": "aud_ghost_0001",
    "schema_version": "1.0",
    "sequence": 1,
    "actor": {"id": "sys_ghost_factory_demo", "kind": "system"},
    "action": "event.ingested",
    "target": {"type": "event", "id": "evt_ghost_0001"},
    "timestamp": "2026-10-19T10:00:01Z",
    "request_id": "req-ghost-0001",
    "outcome": "success",
    "reason": "event accepted; no redaction rules fired",
    "entry_hash": "sha256:" + "a" * 64,
}

_DISPOSITION_EXAMPLE: dict[str, Any] = {
    "id": "dsp_ghost_0001",
    "schema_version": "1.0",
    "finding_id": "fnd_ghost_0001",
    "outcome": "rejected",
    "rationale": (
        "Counterevidence ev_ghost_002 shows the required integration test passed on commit abc123, "
        "so the premise that verification was missing does not hold."
    ),
    "decided_by": "principal_operator",
    "decided_at": "2026-10-19T12:00:00Z",
    "verification_run_ids": ["vbj_ghost_0001"],
    "policy_version": "1.0",
    "supersedes_disposition_id": None,
}

_CASES = [
    pytest.param("system", MonitoredSystem, "system-manifest.json", None, id="system"),
    pytest.param("event", Event, "event.json", None, id="event"),
    pytest.param("claim", Claim, "claim.json", None, id="claim"),
    pytest.param("evidence", EvidenceItem, "evidence.json", None, id="evidence"),
    pytest.param("audit", AuditEntry, None, _AUDIT_EXAMPLE, id="audit"),
    pytest.param("rebuttal", Rebuttal, "rebuttal.json", None, id="rebuttal"),
    pytest.param("disposition", Disposition, None, _DISPOSITION_EXAMPLE, id="disposition"),
]


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _model_accepts(model: type[BaseModel], document: dict[str, Any]) -> bool:
    try:
        model.model_validate(document)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize(("name", "model", "example_name", "inline"), _CASES)
def test_example_is_valid_under_both_schema_and_model(
    name: str,
    model: type[BaseModel],
    example_name: str | None,
    inline: dict[str, Any] | None,
) -> None:
    schema = _load(SCHEMAS / f"{name}.schema.json")
    example = inline if inline is not None else _load(EXAMPLES / example_name)

    Draft202012Validator(schema).validate(example)
    model.model_validate(example)


@pytest.mark.parametrize(("name", "model", "example_name", "inline"), _CASES)
def test_required_fields_agree(
    name: str,
    model: type[BaseModel],
    example_name: str | None,
    inline: dict[str, Any] | None,
) -> None:
    schema = _load(SCHEMAS / f"{name}.schema.json")
    validator = Draft202012Validator(schema)
    example = inline if inline is not None else _load(EXAMPLES / example_name)

    for field_name, field in model.model_fields.items():
        if not field.is_required():
            continue
        mutated = copy.deepcopy(example)
        mutated.pop(field_name, None)

        assert not validator.is_valid(mutated), f"{name}: schema accepted a missing {field_name}"
        assert not _model_accepts(model, mutated), f"{name}: model accepted a missing {field_name}"


@pytest.mark.parametrize(("name", "model", "example_name", "inline"), _CASES)
def test_unknown_fields_agree(
    name: str,
    model: type[BaseModel],
    example_name: str | None,
    inline: dict[str, Any] | None,
) -> None:
    schema = _load(SCHEMAS / f"{name}.schema.json")
    validator = Draft202012Validator(schema)
    example = inline if inline is not None else _load(EXAMPLES / example_name)
    mutated = copy.deepcopy(example)
    mutated["__unexpected_field__"] = "value"

    assert not validator.is_valid(mutated)
    assert not _model_accepts(model, mutated)


@pytest.mark.parametrize(("name", "model", "example_name", "inline"), _CASES)
def test_schema_only_required_fields_are_defaulted_by_the_model(
    name: str,
    model: type[BaseModel],
    example_name: str | None,
    inline: dict[str, Any] | None,
) -> None:
    """Any field the schema requires beyond the model must have a model default.

    Those are server-assigned or collection defaults (``id``, ``received_at``, ``labels``, ...).
    The difference is expected and must stay limited to defaulted fields.
    """

    schema = _load(SCHEMAS / f"{name}.schema.json")
    schema_required = set(schema.get("required", []))
    model_required = {
        field_name for field_name, field in model.model_fields.items() if field.is_required()
    }

    assert model_required <= schema_required
    for field_name in schema_required - model_required:
        assert not model.model_fields[field_name].is_required(), (
            f"{name}.{field_name} is required by the schema but has no model default"
        )
