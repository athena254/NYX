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

Eight cases were approved by the project owner (reviewer `kallmedis`, 26 October 2026) and
scored through the product path:

```bash
nyx golden approve <case_id> --reviewer kallmedis      # x8
nyx golden export                                     # 8 approved cases -> evals/golden/golden.jsonl
uv run python scripts/eval_llm_critic.py --corpus evals/golden/golden.jsonl
```

Recorded in `docs/implementation/evidence/golden-set-scored-2026-10-26-final.json`:

```
deterministic: 8 cases, 8 correct, accuracy 1.0
  acceptance-each-violation-identifies-the-exact-p  expected=finding  det=finding
  any-model-prompt-must-tell-the-model-that-eviden  expected=finding  det=finding
  avoid-metric-labels-containing-user-ids-full-cla  expected=finding  det=finding
  criticism-identify-contradictions-unsupported-cl  expected=finding  det=finding
  evidence-storage-must-preserve-record-identity-a  expected=finding  det=finding
  recommended-approach-do-not-call-every-evaluator  expected=finding  det=finding
  replaying-the-same-accepted-event-must-not-creat  expected=finding  det=finding
  unauthorized-remediation-nyx-identifies-a-seriou  expected=finding  det=finding
model:        scored 0 -- "not_run", not a fabricated score
```

`nyx golden list` reports `total 8, scorable 8, pending 0`. The model leg reports `not_run`
because a golden-set case carries no recorded model response — a skipped leg is excluded from
precision and recall rather than counted as a false negative.

Two earlier cases, approved before the prompt fix, were superseded by the same clause ids being
re-drafted under the corrected templates; the log is append-only and keeps both decisions.

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

Reproduced directly, four clauses, four times. The same clause texts are accepted when sent
individually, and a 4-second pause between clauses did not clear the block, so **the exact
trigger is not settled**. What is settled:

- The block is recorded per clause, with its reason, rather than averaged away.
- It is a property of the provider, not of the case generator: the generator's own contract
  checks all pass before the call is made.
- The two clauses that do get through are not the two simplest — so this is not a length limit.

This is a finding **about dmr-X**, and it is the kind of thing NYX exists to report: a provider
whose injection guardrail refuses most of a specification's own security requirements cannot be
used to build a golden set from that specification.

### 3b. Getting past it — found, measured, and fixed

The block was **the length of the instruction block**, and fixing it took the yield from 2 cases
to 8.

`docs/implementation/evidence/guardrail-experiment-{2,3,4,5,6,7}.txt` records six experiments.
The signal that held across them:

| Prompt | Result on clauses the full prompt refused |
| --- | --- |
| Full instruction block (~1600 chars) + schema + clause | 3 of 3 refused (HTTP 400, deterministic — nine attempts, nine refusals) |
| Clause alone | 3 of 3 accepted |
| Clause + short framing | 3 of 3 accepted |
| Compact instruction (~700 chars) + schema + clause | 3 of 3 accepted |

So the trigger was verbosity, not content: a guardrail reading a long instruction block full of
"must not" and "do not" as an injection attempt. The fix was to say each rule once.

**Nothing safety-relevant was traded away**, and a test pins it:
`test_the_prompt_keeps_the_defence_the_schema_and_the_fence` asserts the injection defence
sentence, the schema, and the untrusted-data fence are all still present, and
`test_the_instruction_block_is_short` fails if the block grows back past 1200 characters.

Measured effect on `docs/SPEC.md`:

| Run | Prompt | Drafted | Contract refusals | Transport refusals |
| --- | --- | --- | --- | --- |
| v9 | 1.5.0, long instruction | 2 | 0 | 15 |
| v11 | 1.6.0, compact instruction | 9 | 0 | 7 |
| v12 | 1.6.0 + typed scope | 8 | 0 | 8 |

All 8 cases from v12 **seed and score correctly** — 8 cases, 8 correct, accuracy 1.0 on the
deterministic baseline.

Two candidate fixes were tried and rejected on evidence rather than kept on hope:

- **Pacing** (`NYX_GOLDEN_DELAY_SECONDS`): did not clear the block. Kept anyway, because it
  costs 34 s on a 17-clause spec and it turns a burst into a conversation — but it is a
  mitigation with no measured effect, and the settings field says so.
- **A shape-based redaction exemption** (commit `fcf9d79`): a 40-character hyphenated
  lowercase *secret* is shape-identical to a slug, so the exemption had to be key-based.
  `test_a_hyphenated_lowercase_secret_is_still_caught` is the test that caught it.

### 3c. The draft schema now describes the consumer's scope

Unblocking the guardrail exposed the next layer: the draft schema left `scope` free-form, so
the model invented shapes the ingestion schema refused at seed time. Three refusals, three
fixes, all in the same shape — *the consumer's contract belongs in the draft schema, not in the
seed's error*:

| Refusal at seed time | Fix |
| --- | --- |
| `Input should be a valid dictionary or instance of Period` — the model sent `"period": "2023-01-01T00:00:00Z/2023-01-02T00:00:00Z"`, a string | The draft schema describes `period` as `{from, to}` |
| `datetime must be timezone-aware` — the model sent `"from": "2023-01-01T00:00:00"` with no offset | The draft schema requires an offset; the generator validates against the real `Period` |
| `unknown_field` / `required` on the scope key | The draft schema requires `system_id` and forbids other keys |

The contract checks now run on the raw payload **before** schema validation, so a violation is
reported as the rule the model broke (`missing_system_placeholder`) rather than as a schema
path. The schema is still enforced afterwards and still catches everything the checks do not.

### 3d. A second source document

The two approved cases were both read from `docs/SPEC.md` and labelled from the same document,
so they cannot certify NYX against that spec (ADR 0013). The remedy tried was a **different
source**: `docs/implementation/critic-catalogue-research.md`, whose requirements were derived
from reading the code, not from the spec.

```
nyx golden generate --spec docs/implementation/critic-catalogue-research.md \
    --log evals/golden/catalogue-cases.jsonl
```

23 clauses, 1 drafted, 14 refused by the same guardrail. The one case —
`retraction-grounds-cope-findings-are-unreliable-e86194ff` — seeds and scores correctly
(1 case, 1 correct, accuracy 1.0). It is a draft, awaiting review.

This is progress on independence and no progress on volume: the guardrail binds on any
source document, not on `docs/SPEC.md`.

## 4. What this does not establish

- **The golden set is three cases, not twenty.** The brief asked for at least 20 labelled cases.
  Two were drafted from `docs/SPEC.md` and approved by the owner; one more was drafted from the
  critic catalogue and is awaiting review. The provider's refusal of most clauses is the binding
  constraint — see §3 — and it is a finding about dmr-X, not a defect in NYX.
- **The two approved cases are not independent of the specification they were read from.** ADR
  0013 records this: the inputs are drafted from the spec and the label is derived from the same
  spec's wording. Two different mechanisms, one source. The reviewer confirmed both, which is
  what makes them count — but they cannot certify NYX against the spec it was written from. The
  catalogue case in §3c is a first step away from that, and it is one case.
- **The labelling rule is untested against a real reviewer.** Its bias toward `finding` (17
  `finding`, 0 `none`, 0 `inconclusive` on `docs/SPEC.md`) is a property of the spec's prose. Two
  cases were reviewed and both kept their proposed `finding` label, which is two data points,
  not a calibration of the rule.
- **The clause unit is fixed but untested against a live run.** The paragraph-joining fix
  (commit `6d8019c`) was verified against `docs/SPEC.md` — 69 clauses, no fragments — but the
  generation runs that produced cases predate it, so no case has been drafted from a
  post-fix clause.
- **The provider block is not explained.** See §3 and §3b.
