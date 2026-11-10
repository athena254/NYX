# NYX v0.0.2 held-out evaluation corpus

A frozen set of labelled scenarios for regression scoring. `scripts/eval_v002_corpus.py` scores
the deterministic critic pack against it; `scripts/v002_acceptance_harness.py` drives the cases
that only exist across a process boundary; `tests/contract/test_v002_corpus.py` pins the shape
that makes both worth running.

## Shape

One JSON object per line in `cases.jsonl`:

| Field | Meaning |
| --- | --- |
| `id` | Stable identifier, unique, prefixed `v002-` |
| `category` | `positive` (a real issue to catch), `negative` (a clean case to leave alone), `unknown` (insufficient or contradictory), `adversarial` (an attempt to trick or overrun) |
| `label` | One sentence of ground truth, written from the SPEC and the critic contracts |
| `source` | Where the scenario came from (`spec:…`, `v002-plan:…`) |
| `expected` | `finding`, `none`, `inconclusive`, `refused`, `allowed`, `masked` |
| `input` | What the runner seeds: `claim`, `claims`, `event`, `evidence`, `observations`, and for request cases `request` + `setup` |
| `critic` | The critic the case belongs to; absent means the request-level cases |
| `flag` | An opt-in Settings flag the case is labelled under |
| `audit_gap` | The reproduced audit gap the case keeps covered, if any |
| `note` | A recorded limitation or a label-provenance decision |

Category and verdict are deliberately not the same axis. `positive` means "NYX must catch this",
`negative` means "NYX must not flag this", `unknown` means "NYX must decline", and `adversarial`
means "the attempt must be handled" — which can legitimately end in any verdict, because the
property under test is the handling.

## Label provenance

Every label was written from the SPEC and the critic's declared scope **before** the
implementation was consulted. The first scoring run disagreed on six cases; each was resolved by
reading the critic, and the resolution is recorded here rather than only in a commit message.

| Case | Label at first run | Resolved | Reason |
| --- | --- | --- | --- |
| `v002-c002-01` | `finding` | case changed | The strict same-scope test requires both claims to declare an environment; without one the case never reached the comparison. The case was corrected to exercise the intended path, and the label stands. |
| `v002-c006-02` | `finding` | `none` | C-006's RULE 2 (a reference that does not authorise this action) is gated by `require_valid_authorisation` and needs a wired authorisation lookup. Under the default rule a reference is enough to keep RULE 1 quiet. Labelled for the default configuration. |
| `v002-c012-04` | `none` | `finding` | C-012's RULE 1 compares declared targets against the allow-list and does not filter by verb. The module docstring's "an unrecognised verb is NOT a scope violation" describes RULE 2 and overstates RULE 1. **Recorded as a documentation mismatch.** |
| `v002-adv-02` | `none` | `finding` | The deterministic outcome reader matches word-boundary markers in free text, so an injected "failed" changes the verdict. The injection defence is on the model path. **Recorded as a limitation of the evidence model.** |
| `v002-c004-01/08` | `none` | case changed | `producer_identity` on a stored record is the *trusted submitter*, not the producer's self-assertion, so a case about complete provenance must name who submitted the evidence. |
| `v002-c001s-09/14` | `finding` | case changed | Same reason: the self-reported branch is only reachable when the submitter is the claimant. |

Two of the six are implementation findings rather than label corrections. Both are recorded in
the case `note` and carried to the review; neither was absorbed silently into a label.

## Independence

A corpus whose labels were copied from the implementation's current output would score 1.0
forever and prove nothing. Three things guard against that:

1. `tests/contract/test_v002_corpus.py` asserts no single verdict dominates, every category is
   populated, and the adversarial cases are not all handled identically.
2. The label provenance table above records every disagreement and how it was settled.
3. An independent review of the corpus against the SPEC is part of the release acceptance.

## Running

```bash
uv run python scripts/eval_v002_corpus.py
uv run python scripts/eval_v002_corpus.py \
  --flag require_independent_verification \
  --flag require_coverage_scope \
  --flag require_verifiable_origin
uv run python scripts/v002_acceptance_harness.py --port 8099
uv run python scripts/v002_outage_probe.py
```

A run scores only the cases labelled under its own configuration: with no `--flag` that is the
default pack, and with `--flag X` it is the pack with X active. Scoring a case under a
configuration it was not labelled under compares two different products.

## Status

Frozen at 132 cases. Coverage of the ten reproduced audit gaps (GAP-01 … GAP-10) is asserted by
`test_every_reproduced_audit_gap_is_represented`.