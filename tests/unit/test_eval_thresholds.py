"""Thresholds are a recorded regression floor, never an invented calibration claim.

While no live run has been recorded the file must say so explicitly. Once a live run lands, the
numeric floors must be well formed and comparable to the runner's metrics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
THRESHOLDS = REPO / "evals" / "llm-critic" / "thresholds.json"
CORPUS = REPO / "evals" / "llm-critic" / "corpus.jsonl"


def _thresholds() -> dict[str, Any]:
    return json.loads(THRESHOLDS.read_text(encoding="utf-8"))


def test_thresholds_reference_the_checked_in_corpus() -> None:
    thresholds = _thresholds()

    assert (REPO / thresholds["corpus"]).resolve() == CORPUS.resolve()


def test_pending_thresholds_carry_no_metrics_and_name_their_open_items() -> None:
    thresholds = _thresholds()

    assert thresholds["status"] in {"pending", "current"}
    if thresholds["status"] == "pending":
        assert thresholds["metrics"] is None, "pending thresholds must not invent numbers"
        assert thresholds["open_items"], "pending thresholds must say what would close them"
        assert "live" in thresholds["note"].lower()
    else:
        assert isinstance(thresholds["metrics"], dict)


def test_current_thresholds_are_numeric_and_in_range() -> None:
    metrics = _thresholds()["metrics"]
    if metrics is None:
        return

    accuracy = metrics.get("min_accuracy")
    assert isinstance(accuracy, (int, float))
    assert 0.0 <= float(accuracy) <= 1.0
    for key in ("max_false_positives", "max_false_negatives"):
        value = metrics.get(key)
        assert isinstance(value, (int, float)) and float(value) >= 0.0
