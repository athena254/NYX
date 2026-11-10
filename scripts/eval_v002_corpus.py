"""Score the NYX v0.0.2 held-out corpus against the deterministic critic pack.

The corpus is a frozen set of labelled scenarios (``evals/v0.0.2-corpus/cases.jsonl``). Each case
carries the ground-truth label written from the SPEC and the critic contracts before the
implementation was consulted. This runner seeds each case into a throwaway database, runs the
critic the case names, and reports where the pack's verdict disagrees with the label.

Disagreement is **not** automatically a bug, and it is not automatically a label error. Each one
has to be resolved by reading the critic: a label that mis-stated the rule is corrected, and a
critic that contradicts its declared scope is a finding. The runner prints both counts so neither
can be quietly absorbed into the other.

Cases that name a ``flag`` are only meaningful when that Settings flag is active. Pass
``--flag NAME`` to score them; without it they are reported as ``not_run`` rather than scored,
because scoring a flag-gated behaviour with the flag off would manufacture a failure.

Request-level cases (``input.request``) are exercised by ``scripts/v002_acceptance_harness.py``
against a live service, not here.

Output is one JSON document on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from nyx.config import Settings  # noqa: E402
from nyx.critics import register_ground_truth, register_storage_backed  # noqa: E402
from nyx.critics.registry import run_selected  # noqa: E402
from nyx.domain.base import PrincipalRef  # noqa: E402
from nyx.domain.enums import CriticResultStatus  # noqa: E402
from nyx.domain.observation import Observation  # noqa: E402
from nyx.ingestion.service import IngestionService  # noqa: E402
from nyx.storage.db import (  # noqa: E402
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from nyx.storage.repositories.observations import ObservationRepository  # noqa: E402

CORPUS = REPO / "evals" / "v0.0.2-corpus" / "cases.jsonl"

# The corpus is written against a fixed synthetic timeline. Using the real clock would make
# every time-dependent label drift with the date the suite happens to run on, which is exactly
# the kind of unreproducible label a held-out corpus exists to prevent.
EVALUATION_TIME = datetime(2026, 10, 24, 10, 0, 0, tzinfo=UTC)

_FAMILY = {
    CriticResultStatus.FINDING: "finding",
    CriticResultStatus.NO_FINDING: "none",
    CriticResultStatus.INCONCLUSIVE: "inconclusive",
}


def load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with CORPUS.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if line.strip():
                case = json.loads(line)
                case["_line"] = number
                cases.append(case)
    return cases


def _migrate(directory: Path, database: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NYX_")}
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


def _substitute(value: Any, system_id: str, claim_id: str) -> Any:
    if isinstance(value, dict):
        return {k: _substitute(v, system_id, claim_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, system_id, claim_id) for v in value]
    if value == "@system":
        return system_id
    if value == "@claim":
        return claim_id
    return value


def seed_case(
    service: IngestionService,
    factory: Any,
    case: dict[str, Any],
    index: int,
) -> str:
    """Persist one corpus case and return the event id the critic runs against."""

    payload = case["input"]
    system = service.register_system(
        {
            "name": f"eval-{index}",
            "kind": "agent",
            "environment": "development",
            "owner": "eval",
            "adapter": "http",
        },
        request_id=f"eval-{index}-sys",
    )
    assert system.record_id is not None, system.error
    system_id = system.record_id

    claim_specs = []
    if payload.get("claim"):
        claim_specs.append(payload["claim"])
    claim_specs.extend(payload.get("claims", []))

    claim_ids: list[str] = []
    for position, spec in enumerate(claim_specs):
        spec = _substitute(dict(spec), system_id, "")
        spec["claimant_system_id"] = system_id
        spec["producer_event_id"] = f"eval-{index}-claim-{position}"
        result = service.register_claim(spec, request_id=f"eval-{index}-claim-{position}")
        assert result.record_id is not None, result.error
        claim_ids.append(result.record_id)

    event_spec = _substitute(dict(payload.get("event") or {}), system_id, claim_ids[0])
    event_spec.update(
        {
            "source_system_id": system_id,
            "occurred_at": "2026-10-24T10:00:00Z",
            "correlation_id": f"eval-{index}-corr",
            "payload": {"claim_id": claim_ids[0]},
            "producer_event_id": f"eval-{index}-event",
            "trust_class": "observed",
            "labels": ["v002-eval"],
            "retention_class": "standard",
        }
    )
    if isinstance(payload.get("event"), dict) and isinstance(payload["event"].get("payload"), dict):
        event_spec["payload"].update(
            _substitute(payload["event"]["payload"], system_id, claim_ids[0])
        )
    event = service.ingest_event(event_spec, request_id=f"eval-{index}-event")
    assert event.record_id is not None, event.error
    event_id = event.record_id

    for position, spec in enumerate(payload.get("evidence", [])):
        spec = _substitute(dict(spec), system_id, claim_ids[0])
        spec.setdefault("source_system_id", system_id)
        spec.setdefault("relevance_to_claim", claim_ids[0])
        spec["producer_event_id"] = f"eval-{index}-evidence-{position}"
        # `producer_identity` on a stored record is the *trusted* submitter, not the producer's
        # own claim: a claimant cannot verify itself by asserting an identity. A case that wants
        # complete provenance therefore names who submitted it.
        submitted_by = (
            PrincipalRef.model_validate(_substitute(payload["submitter"], system_id, claim_ids[0]))
            if payload.get("submitter")
            else None
        )
        result = service.register_evidence(
            spec,
            request_id=f"eval-{index}-evidence-{position}",
            submitted_by=submitted_by,
        )
        assert result.record_id is not None, result.error

    with session_scope(factory) as session:
        for position, spec in enumerate(payload.get("observations", [])):
            spec = _substitute(dict(spec), system_id, claim_ids[0])
            spec.setdefault("id", f"obs_{index}_{position}")
            spec.setdefault("collected_at", EVALUATION_TIME)
            ObservationRepository(session).add(Observation.model_validate(spec))

    return event_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--flag",
        action="append",
        default=[],
        help="enable a Settings flag for this run (repeatable); cases naming it become scoreable",
    )
    parser.add_argument(
        "--case", action="append", default=[], help="score only the named case id (repeatable)"
    )
    args = parser.parse_args()

    cases = load_cases()
    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case["id"] in wanted]

    # A run has one configuration. A case is scored only when the configuration it was labelled
    # under is the one in force: with no --flag that is the default pack, and with --flag X it is
    # the pack with X active. Scoring a case under a configuration it was not labelled under
    # compares two different products and manufactures disagreements.
    enabled = set(args.flag)
    if enabled:
        cases = [case for case in cases if case.get("flag") in enabled]
    else:
        cases = [case for case in cases if case.get("flag") is None]

    directory = Path(tempfile.mkdtemp(prefix="nyx-v002-corpus-"))
    database = directory / "corpus.sqlite3"
    _migrate(directory, database)

    settings = Settings(
        database_url=f"sqlite:///{database.as_posix()}",
        data_dir=directory,
        redaction_enabled=True,
    )
    for flag in args.flag:
        if not hasattr(settings, flag):
            print(f"unknown Settings flag: {flag}", file=sys.stderr)
            return 2
        setattr(settings, flag, True)

    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    service = IngestionService(factory, settings)

    register_storage_backed(factory)
    register_ground_truth(settings)

    rows: list[dict[str, Any]] = []
    try:
        for index, case in enumerate(cases):
            if "request" in case["input"]:
                rows.append(
                    {
                        "id": case["id"],
                        "category": case["category"],
                        "expected": case["expected"],
                        "predicted": "not_run",
                        "reason": "request-level case: exercised by v002_acceptance_harness.py",
                    }
                )
                continue

            flag = case.get("flag")
            if flag and flag not in args.flag:
                rows.append(
                    {
                        "id": case["id"],
                        "category": case["category"],
                        "expected": case["expected"],
                        "predicted": "not_run",
                        "reason": f"requires --flag {flag}",
                    }
                )
                continue

            event_id = seed_case(service, factory, case, index)
            critic = case.get("critic")
            critic_ids = [critic] if critic else None
            with session_scope(factory) as session:
                outcomes = run_selected(
                    session,
                    event_id=event_id,
                    critic_ids=critic_ids,
                    clock=lambda: EVALUATION_TIME,
                )
            outcome = outcomes[0] if outcomes else None
            predicted = _FAMILY.get(outcome.result, "error") if outcome is not None else "not_run"
            rows.append(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "expected": case["expected"],
                    "predicted": predicted,
                    "critic": critic,
                    "flag": flag,
                    "rationale": outcome.rationale if outcome is not None else None,
                }
            )
    finally:
        engine.dispose()

    def score(subset: list[dict[str, Any]]) -> dict[str, Any]:
        scored = [row for row in subset if row["predicted"] != "not_run"]
        correct = [row for row in scored if row["predicted"] == row["expected"]]
        return {
            "cases": len(subset),
            "scored": len(scored),
            "correct": len(correct),
            "accuracy": round(len(correct) / len(scored), 4) if scored else None,
            "disagreements": [row["id"] for row in scored if row["predicted"] != row["expected"]],
        }

    report = {
        "corpus": str(CORPUS.relative_to(REPO)).replace("\\", "/"),
        "evaluation_time": EVALUATION_TIME.isoformat(),
        "flags": args.flag,
        "overall": score(rows),
        "by_category": {
            category: score([row for row in rows if row["category"] == category])
            for category in ("positive", "negative", "unknown", "adversarial")
        },
        "cases": rows,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
