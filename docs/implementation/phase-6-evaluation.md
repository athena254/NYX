# Phase 6 evaluation — model critic vs the deterministic baseline

**Status:** two live runs recorded (26 October 2026), thresholds `current`. The model leg is
weaker than the deterministic baseline on this corpus; the numbers and the reason are in §4.

- Corpus: `evals/llm-critic/corpus.jsonl` (8 labelled cases)
- Runner: `scripts/eval_llm_critic.py`
- Thresholds: `evals/llm-critic/thresholds.json`
- Prompt template under evaluation: c011 `PROMPT_TEMPLATE_VERSION = 1.0.0`
  (`src/nyx/critics/llm.py`), unchanged by this work. The golden-set *drafting* template is a
  separate version in `src/nyx/evals/generate.py`; it does not affect this corpus.

## 1. What is measured

Each case is a synthetic system/claim/event/evidence bundle with a human-assigned expected
outcome family:

- `finding` — a reviewer should be shown an issue;
- `none` — the evidence supports the claim within the critic's scope;
- `inconclusive` — the evidence cannot settle the question.

The runner scores two paths on the same cases:

1. **Deterministic baseline** — every registered deterministic critic runs through the real
   registry. Any `finding` maps to `finding`; any `inconclusive`/`error` without a finding maps to
   `inconclusive`; otherwise `none`.
2. **c011** — the model critic runs alone. In fixture mode the transport returns the case's
   recorded response; in live mode it is `SyncCriticTransport` against the configured provider.

Reported metrics: exact accuracy, true/false positives, false negatives, precision, recall,
per-case latency. Cost is reported as tokens by the critic run, not as currency: no price table
is invented.

## 2. Corpus composition

All eight cases are synthetic, derived from the existing acceptance scenarios and the documented
critic scopes. No customer data, transcript, or credential is present.

| Case | Expected | What it exercises |
| --- | --- | --- |
| `same-commit-tests-pass` | none | control: CI evidence names the claimed commit |
| `mismatched-commit` | finding | evidence names `def456`, claim names `abc123` |
| `missing-verification` | finding | completion claim with no linked evidence |
| `stale-evidence` | finding | evidence valid window closed before the assessment |
| `agent-report-only` | finding | claimant's own summary is the only evidence |
| `unsupported-certainty` | finding | "definitely / 100% verified" language |
| `conflicting-evidence` | finding | two CI records for the same commit disagree |
| `insufficient-evidence` | inconclusive | deployment log says nothing about tests |

The corpus deliberately contains all three outcome families: a single-label corpus would score
perfectly for a constant predictor.

## 3. Fixture-mode results (plumbing only)

Command:

```bash
uv run python scripts/eval_llm_critic.py
```

Recorded 24 October 2026, branch `master` (Phase 6 in progress):

| Path | Cases | Correct | Accuracy | TP | FP | FN | Precision | Recall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Deterministic pack | 8 | 6 | 0.75 | 6 | 2 | 0 | 0.75 | 1.00 |
| c011 fixture transport | 8 | 5 | 0.625 | 4 | 0 | 2 | 1.00 | 0.667 |

Per-case disagreements worth naming:

- The deterministic pack raises a finding on `same-commit-tests-pass` and
  `insufficient-evidence`. Its expected labels are `none` and `inconclusive`. That is a real
  precision limit of the deterministic path on this corpus, not a runner bug; the pack is
  intentionally conservative and the labelled family is about what a reviewer should be shown.
- The fixture pseudo-model misses `unsupported-certainty` (returns `no_finding`) and cannot
  propose a finding on `missing-verification` (no evidence to cite, so it refuses). Both are
  false negatives by design of the recorded fixture responses.

**These fixture scores say nothing about a real model.** They validate seeding, registry wiring,
evidence-ID validation, persistence, scoring, and the report shape.

## 4. Live run — performed 26 October 2026

Two runs were recorded against the owner-approved provider, both reported in
`docs/implementation/evidence/`:

```bash
uv run python scripts/eval_llm_critic.py --live
```

| Run | Cases | Correct | Accuracy | TP | FP | FN | Precision | Recall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 (`live-run-2026-10-26.json`) | 8 | 1 | 0.125 | 1 | 0 | 5 | 1.00 | 0.167 |
| 2 (`live-run-2026-10-26-fixed.json`) | 8 | 1 | 0.125 | 0 | 0 | 6 | — | 0.00 |
| Deterministic baseline (both runs) | 8 | 6 | 0.75 | 6 | 2 | 0 | 0.75 | 1.00 |

Both runs scored c011 at `PROMPT_TEMPLATE_VERSION = 1.0.0` (`src/nyx/critics/llm.py`), which this
work does not change. Run 1 predates the plumbing fixes; run 2 is the measurement the thresholds
come from.

**Provider:** dmr-X (`http://127.0.0.1:3000`), model id `openai-fast`, which resolved to
`gpt-oss-20b`. Token usage is recorded per critic run in the database, not here: no price table is
invented.

### What the runs actually show

1. **Three cases in run 1 failed as "not valid JSON" — and the model had answered them.** dmr-X
   wraps its answer in a ```` ```json ```` fence. `json.loads` raised `Expecting value` on the fence
   and the case was recorded as a transport failure. That is a plumbing defect presenting as a
   model-quality result, and it is fixed: `nyx.llm.provider.extract_json_document` removes the
   wrapper before the parse, in both the model critic and the golden-set generator. The output
   schema is still enforced afterwards, so the fix widens what is *parsed*, never what is
   *accepted* — `tests/unit/test_llm_json_extraction.py` asserts both directions.
2. **The model abstains.** In run 2 it returned `inconclusive` on six of eight cases and proposed
   no finding at all. On `conflicting-evidence` it explained, at length, that the evidence "neither
   confirms nor refutes the event". Abstention is the safe direction and it is the critic's
   documented behaviour, so this is not a defect — but it means the model leg currently contributes
   less than the deterministic pack on this corpus, and the numbers say so rather than hiding it.
3. **The model leg is not deterministic.** `conflicting-evidence` failed as invalid JSON in run 2
   and returned valid JSON on a direct re-probe of the same case. Two runs of eight cases are a
   sample, not a measurement.

### Thresholds

`evals/llm-critic/thresholds.json` is now `current` rather than `pending`. The floors are the
measured values with the runs they came from recorded alongside them, and the file says plainly
that `min_accuracy: 0.125` over eight cases is a collapse floor, not a quality claim.

## 5. Limits

- Eight synthetic cases cannot establish calibration. One case is 0.125 of the score, so the
  threshold catches a collapse, not a drift.
- The deterministic baseline is scored on the same human labels; disagreement is expected where
  the deterministic pack is stricter than the label.
- The model leg abstains on most cases. Whether that is a property of the model, of the prompt, or
  of the corpus's synthetic evidence is **not settled** by these two runs.
- Unsupported assertions are prevented structurally (unlinked evidence IDs make a run
  `inconclusive`) and tested in `tests/security/test_llm_critic_injection.py`; they are not
  estimated from this corpus.
- Two runs against one provider on one day are a sample. The model-quality claim this repository
  makes is the narrow one: the plumbing works, the failure modes are fail-closed, and the measured
  accuracy is 0.125 on this corpus.
