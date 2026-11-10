"""Integrity checks for the frozen v0.0.2 held-out evaluation corpus.

The corpus exists to catch regressions, so the properties that make it worth running are worth
pinning:

* it is large enough and balanced enough that a score means something;
* every case is well-formed and names the critic it belongs to;
* every reproduced audit gap is represented, so a fixed bug cannot silently return;
* the labels are **independent** of the implementation. A corpus whose labels were copied from
  the code's current output would score 1.0 forever and prove nothing, so the test asserts the
  structural signs of independence: no single verdict dominates, every category is populated,
  and the adversarial cases are not all the same verdict.

The empirical half — running the pack and reporting agreement — is ``scripts/eval_v002_corpus.py``.
This module pins the shape that makes that run meaningful.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "evals" / "v0.0.2-corpus" / "cases.jsonl"

CATEGORIES = {"positive", "negative", "unknown", "adversarial"}
VERDICTS = {"finding", "none", "inconclusive", "refused", "allowed", "masked"}

#: The reproduced audit gaps the corpus must keep covered. Each entry is a real defect that was
#: found and fixed in v0.0.2; a case named for it fails if the defect returns.
AUDIT_GAPS = {
    "GAP-01": "protected routes accepted a missing or invalid API key",
    "GAP-02": "a restricted marker was disclosed through a derived output",
    "GAP-03": "a disposition accepted an unknown or unrelated reference",
    "GAP-04": "a probe that did not observe was treated as positive completion support",
    "GAP-05": "an audit checkpoint comparison trusted stored hashes over recomputed bodies",
    "GAP-06": "a model prompt omitted the assessment target it was meant to assess",
    "GAP-07": "work accepted but not yet assessed was lost when the process stopped",
    "GAP-08": "a pending tool call was recorded as completed tool evidence",
    "GAP-09": "a probe could exceed its hard deadline and mutate state after timeout",
    "GAP-10": "the core CLI imported an optional conversational package",
}

#: The category each verdict family belongs to. Adversarial cases may legitimately end in any
#: verdict, because the property under test is how the attempt is handled rather than what it
#: asserts.
_CATEGORY_VERDICTS = {
    "positive": {"finding"},
    "negative": {"none", "allowed"},
    "unknown": {"inconclusive"},
    "adversarial": VERDICTS,
}


def load_cases() -> list[dict[str, Any]]:
    assert CORPUS.exists(), f"corpus missing: {CORPUS}"
    cases: list[dict[str, Any]] = []
    for number, line in enumerate(CORPUS.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            case = json.loads(line)
            case["_line"] = number
            cases.append(case)
    return cases


def test_the_corpus_is_large_enough_and_every_case_is_well_formed() -> None:
    cases = load_cases()
    assert len(cases) >= 100, f"{len(cases)} cases is below the 100-case floor"

    for case in cases:
        case_id = case["id"]
        assert isinstance(case_id, str) and case_id.startswith("v002-"), case_id
        assert case["category"] in CATEGORIES, (case_id, case["category"])
        assert case["expected"] in VERDICTS, (case_id, case["expected"])
        assert isinstance(case["label"], str) and case["label"].strip(), case_id
        assert isinstance(case["source"], str) and case["source"].strip(), case_id

        payload = case["input"]
        assert isinstance(payload, dict), case_id
        if "request" in payload:
            request = payload["request"]
            assert request["method"] in {"GET", "POST", "CLI", "SCENARIO"}, case_id
            assert request["path"], case_id
        else:
            assert isinstance(payload["claim"], dict), case_id
            assert payload["claim"]["statement"], case_id
            assert payload["claim"]["scope"]["system_id"], case_id
            assert isinstance(payload["evidence"], list), case_id
            assert isinstance(payload["observations"], list), case_id
            for item in payload["evidence"]:
                assert item["kind"], case_id
                assert item["source_ref"], case_id
            for item in payload["observations"]:
                assert item["probe_id"], case_id
                assert item["result"] in {"observed", "not_observed", "error", "skipped"}


def test_case_ids_are_unique() -> None:
    cases = load_cases()
    ids = [case["id"] for case in cases]
    duplicates = [case_id for case_id, count in Counter(ids).items() if count > 1]
    assert not duplicates, f"duplicate case ids: {duplicates}"


def test_every_category_is_populated() -> None:
    """A corpus with no negatives cannot detect a critic that fires on everything."""

    counts = Counter(case["category"] for case in load_cases())
    for category in CATEGORIES:
        assert counts[category] >= 5, f"{category} has only {counts[category]} cases"


def test_verdicts_are_consistent_with_their_category() -> None:
    for case in load_cases():
        allowed = _CATEGORY_VERDICTS[case["category"]]
        assert case["expected"] in allowed, (case["id"], case["category"], case["expected"])


def test_no_single_verdict_dominates_the_corpus() -> None:
    """A corpus that is 90% one verdict is a proxy for 'copy the majority class'.

    Independence is not provable from the file alone — that is what the independent review and
    the recorded disagreement resolutions are for — but a corpus a degenerate predictor could
    ace is not worth running, and this assertion is what keeps it from becoming one.
    """

    counts = Counter(case["expected"] for case in load_cases())
    total = sum(counts.values())
    majority, share = counts.most_common(1)[0]
    assert share / total < 0.5, f"{majority} is {share}/{total} of the corpus"


def test_every_reproduced_audit_gap_is_represented() -> None:
    covered = {case.get("audit_gap") for case in load_cases()}
    missing = sorted(set(AUDIT_GAPS) - covered)
    assert not missing, f"audit gaps with no corpus case: {missing}"


def test_audit_gap_references_are_all_known() -> None:
    for case in load_cases():
        gap = case.get("audit_gap")
        if gap is not None:
            assert gap in AUDIT_GAPS, (case["id"], gap)


def test_flagged_cases_declared_a_real_settings_flag() -> None:
    from nyx.config import Settings

    flagged = {case["flag"] for case in load_cases() if isinstance(case.get("flag"), str)}
    for flag in flagged:
        assert hasattr(Settings(), flag), f"unknown Settings flag in corpus: {flag}"


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_each_category_carries_both_outcomes_where_the_verdict_family_allows(category: str) -> None:
    """A category whose cases all expect one verdict cannot detect a regression on the others.

    For the three single-verdict categories this asserts the verdicts stay inside the family the
    category defines; for adversarial it asserts the attempts are not all handled identically,
    because an attack that is always refused and an attack that is always allowed are the same
    blind spot seen from two sides.
    """

    verdicts = {case["expected"] for case in load_cases() if case["category"] == category}
    allowed = _CATEGORY_VERDICTS[category]
    assert verdicts, f"{category} has no cases"
    assert verdicts <= allowed, (category, verdicts)
    if category == "adversarial":
        assert len(verdicts) >= 2, f"adversarial cases all expect {verdicts}"
