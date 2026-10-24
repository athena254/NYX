# Phase 6 evaluation — model critic vs the deterministic baseline

**Status:** plumbing evaluated; **no live model run has been recorded yet.** The thresholds file
is deliberately `pending` and no accuracy number below is a claim about a real provider.

- Corpus: `evals/llm-critic/corpus.jsonl` (8 labelled cases)
- Runner: `scripts/eval_llm_critic.py`
- Thresholds: `evals/llm-critic/thresholds.json`
- Prompt template under evaluation: c011 `PROMPT_TEMPLATE_VERSION = 1.0.0`

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

## 4. Live run — not yet performed

A live run requires explicit owner approval because it sends the synthetic case bundles to
dmr-X. When approved:

```bash
uv run python scripts/eval_llm_critic.py --live
```

Then:

1. record the JSON report here (provider, model id, date, token counts, per-case outcomes);
2. replace `evals/llm-critic/thresholds.json` with measured floors plus a margin;
3. re-run the corpus whenever `PROMPT_TEMPLATE_VERSION` changes.

Provider instability is recorded as a failure to obtain a structured assessment, not as a
critic-quality result (R4 in the Phase 6 plan).

## 5. Limits

- Eight synthetic cases cannot establish calibration. Thresholds are a regression floor, not a
  probability that the critic is right.
- The deterministic baseline is scored on the same human labels; disagreement is expected where
  the deterministic pack is stricter than the label.
- Unsupported assertions are prevented structurally (unlinked evidence IDs make a run
  `inconclusive`) and tested in `tests/security/test_llm_critic_injection.py`; they are not
  estimated from this corpus.
- No model-quality claim is made until a live run and a recorded threshold exist.
