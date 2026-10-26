# Golden-set run — 26 October 2026

**Status:** the mechanism works end to end; **the set is small.** Two cases were drafted from
`docs/SPEC.md`, both seed and score correctly, and both were approved by the project owner on
26 October 2026. Fifteen of the seventeen labelled clauses were refused by the provider. This
document records what was run, what each run proved, and what is still not established.

- Code: `src/nyx/evals/`, commands `nyx golden ...`, runner `scripts/eval_llm_critic.py`
- Spec read: `docs/SPEC.md` → 70 clauses, 17 carrying an outcome signal, 53 with no signal
- Case log: `evals/golden/cases.jsonl`
- Drafting template: `PROMPT_TEMPLATE_VERSION = 1.5.0`

## 1. What was run

Nine `nyx golden generate --spec docs/SPEC.md` runs, each recorded in
`docs/implementation/evidence/`:

| Run | Drafted | Failed | What it proved |
| --- | --- | --- | --- |
| v1 | 12 | 5 | The real path drafted cases — and the model invented a duplicate case id, which made the log unreadable |
| v2 | 12 | 5 | Unique derived ids; 5 failures each named with a reason |
| v3 | 6 | 11 | A scope under the wrong key is refused at draft time |
| v4 | 7 | 10 | An unscoped claim is refused at draft time |
| v5 | 2 | 15 | An invented `event_type` is refused at draft time |
| v6 | 2 | 15 | **Both cases seed and score correctly** |
| v7 | 2 | 15 | Reproducible |
| v8 | 2 | 15 | A partial run is retryable — the two drafts are re-proposed without colliding |
| v9 | 2 | 15 | A 4-second pause between clauses did not clear the provider block |

The two cases that draft reliably are `high-impact-actions-are-recommendations-only-in-…` and
`security-tests-for-unauthorised-access-prompt-in-…`. Both reach the deterministic pack's
expected verdict:

```
uv run python scripts/eval_llm_critic.py --corpus <exported drafts>
  deterministic: 2 cases, 2 correct, accuracy 1.0
  OK  high-impact-actions-are-recommendations-only-in-cf6d  expected=finding  det=finding
  OK  security-tests-for-unauthorised-access-prompt-in-a5f1  expected=finding  det=finding
```

They are **drafts**. Nothing here approves them, and `scorable_cases()` refuses to emit an
unapproved case by any route.

## 1b. The approved set, scored through the product path

After the owner approved both cases (reviewer `kallmedis`, 26 October 2026):

```bash
nyx golden approve <case_id> --reviewer kallmedis
nyx golden export                       # 2 approved cases -> evals/golden/golden.jsonl
uv run python scripts/eval_llm_critic.py --corpus evals/golden/golden.jsonl
```

Recorded in `docs/implementation/evidence/golden-set-scored-2026-10-26.json`:

```
deterministic: 2 cases, 2 correct, accuracy 1.0
  high-impact-actions-are-recommendations-only-in-cf6d  expected=finding  det=finding
  security-tests-for-unauthorised-access-prompt-in-a5f1  expected=finding  det=finding
model:        scored 0 -- "not_run", not a fabricated score
```

`nyx golden list` reports `total 2, scorable 2, pending 0`. The model leg reports `not_run`
because a golden-set case carries no recorded model response — a skipped leg is excluded from
precision and recall rather than counted as a false negative.

## 2. Defects found, and fixed

Each was found by driving the real path. None was reachable from the unit tests, because a
test's fake transport returns exactly the shape the code expects.

| # | Defect | Fix |
| --- | --- | --- |
| D1 | The drafting prompt said "Return JSON matching the schema" and never showed the schema, so the model invented a shape | The schema is embedded in the prompt |
| D2 | A markdown-fenced answer was rejected as "not valid JSON" — a wrapper treated as a refusal | `extract_json_document` removes the wrapper before the parse; the schema is still enforced |
| D3 | `nyx golden generate` printed "3 failed" with no reason | Every failed clause is named with its text and its reason |
| D4 | A 250-character requirement was dropped by a 200-character field | `ClauseRef` bound raised to 2000 |
| D5 | Redaction destroyed a schema-constrained identifier: a 62-character system name became `[REDACTED:high_entropy_value]` and then failed its own schema | `name`/`title`/`slug`/`label` join the entropy rule's identifier-key exemptions |
| D6 | The prompt never stated the consumer's `@system` / `system_id` / scoped-claim contract, so cases were refused at seed time | The contract is in the prompt **and** checked at draft time |
| D7 | `event_type` had no pattern, so the model invented one the ingestion schema refused | The consumer's pattern is in the draft schema |
| D8 | A partial run could not be retried: a re-proposal for the same clause collided with its own earlier draft | Re-proposing a **draft** is allowed; re-proposing a reviewed case is still refused |

**D5 is the one worth remembering.** A 40-character hyphenated lowercase *secret* is
shape-identical to a 62-character hyphenated lowercase *slug*, so the exemption had to be
key-based, not shape-based. A first attempt that exempted the shape was written, tested, and
reverted — `test_a_hyphenated_lowercase_secret_is_still_caught` is the test that caught it.

## 3. What the provider refused, and what is not settled

Fifteen of the seventeen labelled clauses were refused with:

```
HTTP 400  {"error":{"message":"Request blocked by guardrail: Detected System Prompt Injection"}}
```

Reproduced directly, four different clauses, four times. The same clause texts are accepted when
sent individually, and a 4-second pause between clauses did not clear the block, so **the exact
trigger is not settled**. What is settled:

- The block is recorded per clause, with its reason, rather than averaged away.
- It is a property of the provider, not of the case generator: the generator's own contract
  checks all pass before the call is made.
- The two clauses that do get through are not the two simplest — so this is not a length limit.

This is a finding **about dmr-X**, and it is the kind of thing NYX exists to report: a provider
whose injection guardrail refuses most of a specification's own security requirements cannot be
used to build a golden set from that specification.

## 4. What this does not establish

- **The golden set is two cases, not twenty.** The brief asked for at least 20 labelled cases.
  Two drafted, both approved. The provider's refusal of 15 of 17 clauses is the binding
  constraint — see §3 — and it is a finding about dmr-X, not a defect in NYX.
- **The two cases are not independent of the specification they were read from.** ADR 0013
  records this: the inputs are drafted from the spec and the label is derived from the same
  spec's wording. Two different mechanisms, one source. The reviewer confirmed both, which is
  what makes them count — but they cannot certify NYX against the spec it was written from.
- **The labelling rule is untested against a real reviewer.** Its bias toward `finding` (17
  `finding`, 0 `none`, 0 `inconclusive` on `docs/SPEC.md`) is a property of the spec's prose. Two
  cases were reviewed and both kept their proposed `finding` label, which is two data points,
  not a calibration of the rule.
- **`parse_spec` emits fragments.** Several "clauses" are mid-sentence fragments of a prose
  bullet ("reasoning gaps, drift, policy violations, security weaknesses, and"), because a
  clause is a line or bullet. That is a quality problem in the clause unit, not in the
  generation, and it is not fixed here.
- **The provider block is not explained.** See §3.
