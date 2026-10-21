"""The evidence relevance rule, and its one documented escape hatch (ADR 0008).

SPEC §5.4 asks for ``relevance_to_claim`` or ``relevance_to_finding``. SPEC §14 asks for an
adapter that observes a CI run *before* any claim references it. Those two cannot both hold
literally, so the rule is: evidence must name a relevance target **or** carry a
``relevance_note`` explaining why it is unlinked.

That keeps the original refusal intact for a producer that simply omits the field, and makes
the adapter's unlinked evidence say so in the record rather than leaving a reader to infer it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nyx.domain.enums import EvidenceKind, IntegrityMethod, RetentionClass, Sensitivity
from nyx.domain.evidence import EvidenceItem

_NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _evidence(**overrides: object) -> EvidenceItem:
    fields: dict[str, object] = {
        "kind": EvidenceKind.CI_TEST_RESULT,
        "title": "ci / test",
        "description": "All checks passed.",
        "source_system_id": "sys_demo",
        "source_ref": "https://ci.example.invalid/runs/1",
        "captured_at": _NOW,
        "valid_from": _NOW,
        "integrity_method": IntegrityMethod.HASH,
        "limitations": ["Synthetic test evidence."],
        "sensitivity": Sensitivity.INTERNAL,
        "access_policy_ref": "evidence:internal",
        "retention_class": RetentionClass.STANDARD,
    }
    fields.update(overrides)
    return EvidenceItem(**fields)  # type: ignore[arg-type]


def test_evidence_with_a_claim_target_is_accepted() -> None:
    evidence = _evidence(relevance_to_claim="clm_demo")

    assert evidence.relevance_to_claim == "clm_demo"


def test_evidence_with_a_finding_target_is_accepted() -> None:
    evidence = _evidence(relevance_to_finding="fnd_demo")

    assert evidence.relevance_to_finding == "fnd_demo"


def test_evidence_with_no_target_and_no_explanation_is_refused() -> None:
    """The original rule is intact: omitting the field is still an error."""

    with pytest.raises(ValidationError, match="relevance_note"):
        _evidence()


def test_evidence_with_no_target_but_an_explanation_is_accepted() -> None:
    """The adapter path: an observing adapter produces evidence before any claim exists."""

    evidence = _evidence(
        relevance_to_claim=None,
        relevance_to_finding=None,
        relevance_note=(
            "Produced by the git-ci adapter from a CI run. Not linked to a claim at registration."
        ),
    )

    assert evidence.relevance_to_claim is None
    assert evidence.relevance_to_finding is None
    assert "git-ci adapter" in evidence.relevance_note
