# The golden set: drafting cases from a spec, and reviewing them

**Status:** implemented. The code is `src/nyx/evals/`; the commands are `nyx golden ...`; the
runner is `scripts/eval_llm_critic.py`.

## The problem this solves

A critic pack can only be measured against cases somebody wrote. The repository had eight
hand-written synthetic cases in `evals/llm-critic/corpus.jsonl` and no way to grow them, so
"how accurate is C-008?" had no answer that was not a guess.

This layer adds the missing half: read a specification, turn its requirements into labelled
cases, and let a human decide which ones count.

## The three steps, and why the order is the point

### 1. Read the spec into clauses — `nyx.evals.spec`

`parse_spec()` splits a markdown document into *clauses*: lines and bullets that carry a
normative keyword. Headings are tracked so each clause names the section it came from, and
code fences, tables and prose are skipped.

`propose_label()` then reads a verdict out of the clause's **own wording**: a clause about a
violation proposes `finding`, one about a satisfied requirement proposes `none`, one about an
undeterminable outcome proposes `inconclusive`.

Three rules make that conservative rather than clever:

- A clause with **no** outcome signal is skipped, not guessed.
- A clause with **conflicting** signals (both "is a finding" and "passes") is skipped. A
  keyword rule cannot label a clause about two outcomes, and picking the first match would
  hide that.
- **Quoted spans are stripped before matching.** "Detect a claim whose wording is stronger …
  e.g. 'all tests pass' backed by one run" is a requirement about over-strong wording; the
  quoted phrase is the *bad example*. Reading the quote as the clause's signal labels the
  requirement backwards.

### 2. Draft the inputs — `nyx.evals.generate`

`CaseGenerator` turns each clause into a case's **inputs** — a claim an agent might make, an
event that carries it, and the evidence linked to it — through the same LLM seam the model
critic uses.

The verdict is **not the model's to decide**, and the design says so twice: the prompt states
that the outcome has already been determined and must not be mentioned, and the draft schema
has no field for it, so a model cannot smuggle one in.

Fail-closed, like every model-backed component here:

- no transport configured → `CaseGenerationError`, and **no case is produced**;
- invalid JSON, a schema violation, or an unknown evidence kind → no case;
- a clause with no outcome signal → skipped **before** the model is asked, so it costs nothing;
- one unusable clause → reported and skipped, and the rest of the run continues.

Every drafted case is a **draft**, and every draft records its model, prompt version and the
clause it came from.

### 3. Review — `nyx.evals.review`

A draft becomes a test only when a person approves it. `ReviewQueue` is strict about three
things:

- **The reviewer's identity is required.** A review with no named reviewer is refused rather
  than recorded as `system`.
- **A decision about an unknown case is refused.** Approving an id nobody drafted would let a
  typo create a golden case.
- **A rejection needs a reason.** A rejected case is a finding about the requirement — a
  clause that cannot be turned into a test is worth knowing about — so an unexplained
  rejection is a case silently dropped.

Decisions are appended to the log, never rewritten, and the current state is derived by
replaying it. A case proposed twice is refused, because re-proposing would silently revert an
approval to a draft.

## Why a generated case is not a test

A case whose *label* came from the same text as its *inputs* is the source grading itself.
Here the inputs are drafted by a model and the label is derived by a keyword rule — two
different mechanisms — but both read the same specification, so the result is still not
independent of it.

What makes a case evidence is the reviewer confirming, against the requirement, that the
inputs really exercise it and that the proposed label is the right one. That step is the
independence, and it is the only step a person can perform. Everything before it is drafting.

**A case is never scored until it is approved.** `scorable_cases()` is the single function
that decides what the golden set is; nothing else may decide it.

## Commands

```bash
nyx golden generate --spec docs/SPEC.md      # draft cases; requires NYX_LLM_ENABLED + a model
nyx golden pending                           # the review queue
nyx golden list                              # every case, with its status
nyx golden approve <case_id> --reviewer alice --note "..."
nyx golden reject  <case_id> --reviewer alice --note "why"   # --note is required
nyx golden export --out evals/golden/golden.jsonl           # only approved cases
```

Every command takes `--json`. `generate` writes drafts and refuses to write anything when no
provider is configured.

The case log lives at `evals/golden/cases.jsonl` by default; `--log` overrides it.

## Running the golden set

```bash
nyx golden export --out evals/golden/golden.jsonl
uv run python scripts/eval_llm_critic.py --corpus evals/golden/golden.jsonl
```

The runner accepts JSONL (a hand-written corpus) or the JSON array `export` writes. A case
with no `fixture_response` — which is every golden-set case — is scored against the
deterministic pack and reports `"model": "not_run"` rather than inventing a model response.
Its model-leg metrics are `null` with `scored: 0`, not a fabricated score.

## What this does not establish

- **The labelling rule is biased toward `finding`.** Run against `docs/SPEC.md` it proposes
  17 `finding` labels, 0 `none` and 0 `inconclusive`, because the specification is written
  mostly as "X is a finding". The rule is a drafting aid, not a balanced classifier, and the
  review step is what corrects it.
- **No case has been generated against a real spec by a real provider yet.** The drafting path
  is proven by tests with a fake transport and by the fail-closed refusals, not by a recorded
  live run.
- **The golden set is small.** The brief asks for at least 20 labelled cases; the repository
  has eight hand-written ones and this layer has produced none against a live provider. The
  generator is the mechanism, not the set.
- **Precision and recall are per-critic only once the set exists.** The runner reports them;
  it cannot report them for cases nobody has written.
