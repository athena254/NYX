"""The evaluation corpus is data, so its invariants are tested like data.

A malformed line would otherwise surface as a runner crash halfway through a corpus, or worse as
a silently mislabelled case that flatters the model. These tests check shape, unique ids, known
enums, and that every case carries a fixture response whose family is one the critic understands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nyx.domain.enums import ClaimType, EvidenceKind

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "evals" / "llm-critic" / "corpus.jsonl"
FAMILIES = {"finding", "none", "inconclusive"}
REQUIRED_CASE_KEYS = {
    "case_id",
    "source",
    "expected",
    "claim",
    "event",
    "evidence",
    "fixture_response",
}


def _cases() -> list[dict[str, Any]]:
    with CORPUS.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_the_corpus_is_non_empty_and_every_line_parses() -> None:
    cases = _cases()

    assert len(cases) >= 5, "a corpus this small cannot report a rate honestly"
    assert all(case.keys() >= REQUIRED_CASE_KEYS for case in cases)


def test_case_ids_are_unique() -> None:
    case_ids = [case["case_id"] for case in _cases()]

    assert len(case_ids) == len(set(case_ids))


def test_every_case_has_a_known_expected_family() -> None:
    assert {case["expected"] for case in _cases()} <= FAMILIES


def test_every_case_names_its_source() -> None:
    for case in _cases():
        assert case["source"].strip(), f"{case['case_id']} has no source"


def test_claim_and_evidence_enums_are_known() -> None:
    for case in _cases():
        ClaimType(case["claim"]["claim_type"])
        for item in case["evidence"]:
            EvidenceKind(item["kind"])


def test_scopes_use_the_system_placeholder_not_a_persisted_id() -> None:
    """The corpus is seeded per run; a hardcoded system id would point at nothing."""

    for case in _cases():
        assert case["claim"]["scope"]["system_id"] == "@system"
        for item in case["evidence"]:
            assert item["scope"]["system_id"] == "@system"


def test_fixture_responses_are_well_formed() -> None:
    for case in _cases():
        response = case["fixture_response"]
        assert response["result"] in {"finding", "no_finding", "inconclusive"}
        assert response["proposition"].strip()
        assert response["rationale"].strip()
        assert response["recommended_next_step"].strip()
        assert isinstance(response["observations"], list)
        assert isinstance(response["supporting_evidence_ids"], list)
        assert isinstance(response["counterevidence_ids"], list)
        if response["result"] == "finding":
            assert response["supporting_evidence_ids"], (
                f"{case['case_id']} claims a finding but cites no evidence; c011 would reject it"
            )


def test_expected_families_are_not_all_the_same() -> None:
    """A one-label corpus scores perfectly for a constant predictor; that would hide failures."""

    families = {case["expected"] for case in _cases()}

    assert families == FAMILIES, f"the corpus hides at least one outcome family: {families}"


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["case_id"])
def test_evidence_items_carry_a_description(case: dict[str, Any]) -> None:
    for item in case["evidence"]:
        assert item["description"].strip()
