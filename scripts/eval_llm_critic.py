"""Evaluate c011 and the deterministic pack on the labelled corpus.

Default (``--fixture``) needs no provider: each case carries a recorded fixture response, so this
mode validates the evaluation plumbing and scores a fixed, documented pseudo-model. It is NOT a
calibration claim about a real model.

``--live`` sends each case to the configured provider (owner approval required) and reports the
same metrics. Thresholds in ``evals/llm-critic/thresholds.json`` are enforced once they stop being
``"pending"``; until a live run is recorded, the runner reports the deterministic baseline and the
fixture scores without inventing a floor.

Output is one JSON document on stdout. Nothing here prints a credential.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from nyx.config import Settings  # noqa: E402
from nyx.critics.llm import OUTPUT_SCHEMA, LlmCritic  # noqa: E402
from nyx.critics.llm_transport import SyncCriticTransport  # noqa: E402
from nyx.critics.registry import register, run_selected  # noqa: E402
from nyx.domain.enums import CriticResultStatus  # noqa: E402
from nyx.ingestion.service import IngestionService  # noqa: E402
from nyx.llm.provider import LLMConfig  # noqa: E402
from nyx.storage.db import (  # noqa: E402
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)

CORPUS = REPO / "evals" / "llm-critic" / "corpus.jsonl"
THRESHOLDS = REPO / "evals" / "llm-critic" / "thresholds.json"
FAMILIES = {"finding", "none", "inconclusive"}


class FixtureTransport:
    """Returns the corpus case's recorded response, after placeholder substitution."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.last_usage = {"input_tokens": 0, "output_tokens": 0}

    def complete(self, *, prompt: str, schema: dict[str, Any], timeout_seconds: float) -> str:
        return json.dumps(self.response)


def load_corpus() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with CORPUS.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            case = json.loads(line)
            case.setdefault("_line", number)
            cases.append(case)
    return cases


def _migrate(directory: Path, database: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NYX_")}
    env["NYX_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["NYX_DATA_DIR"] = str(directory)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")


def seed_case(service: IngestionService, case: dict[str, Any]) -> tuple[str, list[str]]:
    """Persist one corpus case under the shared evaluation database."""

    case_id = case["case_id"]
    system = service.register_system(
        {
            "name": f"eval-{case_id}",
            "kind": "agent",
            "environment": "development",
            "owner": "eval",
            "adapter": "http",
        },
        request_id=f"eval-{case_id}-sys",
    )
    assert system.record_id is not None, system.error
    system_id = system.record_id

    claim_spec = dict(case["claim"])
    claim_spec["scope"] = _substitute(claim_spec["scope"], system_id, [])
    claim_spec["claimant_system_id"] = system_id
    claim_spec["producer_event_id"] = f"eval-{case_id}-claim"
    claim = service.register_claim(claim_spec, request_id=f"eval-{case_id}-claim")
    assert claim.record_id is not None, claim.error
    claim_id = claim.record_id

    event_spec = dict(case["event"])
    event_spec.update(
        {
            "source_system_id": system_id,
            "occurred_at": "2026-10-24T10:00:00Z",
            "correlation_id": f"eval-{case_id}-corr",
            "payload": {"claim_id": claim_id},
            "producer_event_id": f"eval-{case_id}-event",
            "trust_class": "observed",
            "labels": ["llm-eval"],
            "retention_class": "standard",
        }
    )
    event = service.ingest_event(event_spec, request_id=f"eval-{case_id}-event")
    assert event.record_id is not None, event.error

    evidence_ids: list[str] = []
    for index, item in enumerate(case["evidence"]):
        spec = dict(item)
        spec.update(
            {
                "scope": _substitute(spec["scope"], system_id, []),
                "source_system_id": system_id,
                "source_ref": f"https://eval.example.invalid/{case_id}/{index}",
                "relevance_to_claim": claim_id,
                "limitations": ["Synthetic evaluation evidence."],
                "producer_event_id": f"eval-{case_id}-evidence-{index}",
            }
        )
        evidence = service.register_evidence(spec, request_id=f"eval-{case_id}-evidence-{index}")
        assert evidence.record_id is not None, evidence.error
        evidence_ids.append(evidence.record_id)

    return event.record_id, evidence_ids


def _substitute(value: Any, system_id: str, evidence_ids: list[str]) -> Any:
    if isinstance(value, dict):
        return {key: _substitute(item, system_id, evidence_ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, system_id, evidence_ids) for item in value]
    if value == "@system":
        return system_id
    return value


def _family(results: list[CriticResultStatus]) -> str:
    if any(result is CriticResultStatus.FINDING for result in results):
        return "finding"
    if any(
        result in {CriticResultStatus.INCONCLUSIVE, CriticResultStatus.ERROR} for result in results
    ):
        return "inconclusive"
    return "none"


def score(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    true_positive = false_positive = false_negative = 0
    correct = 0
    for row in rows:
        expected, predicted = row["expected"], row[key]
        if predicted == expected:
            correct += 1
        if predicted == "finding" and expected == "finding":
            true_positive += 1
        elif predicted == "finding":
            false_positive += 1
        elif expected == "finding":
            false_negative += 1
    total = len(rows)
    return {
        "cases": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "true_positives": true_positive,
        "false_positives": false_positive,
        "false_negatives": false_negative,
        "precision": round(true_positive / (true_positive + false_positive), 4)
        if true_positive + false_positive
        else None,
        "recall": round(true_positive / (true_positive + false_negative), 4)
        if true_positive + false_negative
        else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="call the configured provider")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--protocol", default=None, choices=["openai", "anthropic"])
    parser.add_argument("--timeout", type=float, default=None)
    args = parser.parse_args()

    settings = Settings()
    if args.live and not (args.model or settings.llm_model):
        print("NYX_LLM_MODEL is required for --live; the runner will not guess a model")
        return 1

    cases = load_corpus()
    directory = Path(tempfile.mkdtemp(prefix="nyx-llm-eval-"))
    database = directory / "eval.sqlite3"
    _migrate(directory, database)

    eval_settings = Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        data_dir=directory,
        redaction_enabled=True,
    )
    engine = create_engine_from_settings(eval_settings)
    factory = create_session_factory(engine)
    service = IngestionService(factory, eval_settings)

    from nyx.critics import register_storage_backed

    register_storage_backed(factory)

    if args.live:
        config = LLMConfig(
            protocol=(args.protocol or settings.llm_protocol),
            base_url=args.base_url or settings.llm_base_url,
            api_key=(
                settings.llm_api_key.get_secret_value()
                if settings.llm_api_key is not None
                else None
            ),
            model=args.model or settings.llm_model or "",
            timeout_seconds=args.timeout or settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        provider = settings.llm_provider or config.protocol
        model_version = config.model
        transport: Any = SyncCriticTransport(config)
        log_prompts = False
    else:
        provider = "fixture"
        model_version = "fixture-model"
        transport = None
        log_prompts = False

    rows: list[dict[str, Any]] = []
    try:
        for case in cases:
            event_id, evidence_ids = seed_case(service, case)

            with session_scope(factory) as session:
                deterministic = run_selected(session, event_id=event_id)
            deterministic_family = _family([outcome.result for outcome in deterministic])

            if args.live:
                register(
                    LlmCritic(
                        transport=transport,
                        schema=OUTPUT_SCHEMA,
                        timeout_seconds=config.timeout_seconds,
                        model_provider=provider,
                        model_version=model_version,
                        log_prompts=log_prompts,
                    )
                )
            else:
                response = dict(case["fixture_response"])
                response["supporting_evidence_ids"] = [
                    evidence_ids[int(token.split(":")[1])]
                    if token.startswith("@evidence:")
                    else token
                    for token in response.get("supporting_evidence_ids", [])
                ]
                response["counterevidence_ids"] = [
                    evidence_ids[int(token.split(":")[1])]
                    if token.startswith("@evidence:")
                    else token
                    for token in response.get("counterevidence_ids", [])
                ]
                register(
                    LlmCritic(
                        transport=FixtureTransport(response),
                        schema=OUTPUT_SCHEMA,
                        model_provider=provider,
                        model_version=model_version,
                    )
                )

            started = time.monotonic()
            with session_scope(factory) as session:
                model_outcomes = run_selected(
                    session, event_id=event_id, critic_ids=["c011-llm-critic"]
                )
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            outcome = model_outcomes[0]
            rows.append(
                {
                    "case_id": case["case_id"],
                    "source": case["source"],
                    "expected": case["expected"],
                    "deterministic": deterministic_family,
                    "model": outcome.result.value,
                    "model_latency_ms": elapsed_ms,
                    "model_rationale": outcome.rationale,
                }
            )
    finally:
        engine.dispose()

    thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    report = {
        "mode": "live" if args.live else "fixture",
        "provider": provider,
        "model_version": model_version,
        "corpus": str(CORPUS.relative_to(REPO)).replace("\\", "/"),
        "thresholds_status": thresholds.get("status"),
        "deterministic_baseline": score(rows, "deterministic"),
        "model": score(rows, "model"),
        "cases": rows,
    }
    print(json.dumps(report, indent=2))

    floors = thresholds.get("metrics")
    if isinstance(floors, dict) and args.live:
        model_metrics = report["model"]
        minimum = floors.get("min_accuracy")
        if isinstance(minimum, (int, float)) and model_metrics["accuracy"] < minimum:
            print(
                f"THRESHOLD FAILED: model accuracy {model_metrics['accuracy']} < {minimum}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
