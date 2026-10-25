# ADR 0013 — A golden set drafted from a spec, and reviewed by a person

**Status:** accepted, 25 October 2026.

## Context

NYX's critics could only be measured against cases somebody had written by hand. The
repository held eight synthetic cases in `evals/llm-critic/corpus.jsonl`, no way to grow
them, and no answer to "how accurate is C-008?" that was not a guess.

The obvious fix is to read a specification and generate the cases from it. That fix has a
problem, and the problem is the reason this ADR exists rather than a module.

A case generated from a specification, and labelled from the same specification, is the
specification grading itself. It will pass, and the number it produces will mean nothing.
NYX's whole value is that its verdicts are evidence-linked and its evidence is independent of
the thing it judges — a case derived from the spec under test fails that test by construction.

The brief this work implements asks for both halves at once: *"Build a golden set of at least
20 real, labelled cases"* and *"the LLM is the interface, never the judge"*.

## Decision

Split case production into three steps with three different mechanisms, and make the third
one impossible to automate:

1. **Read** the spec into clauses (`nyx.evals.spec`). Deterministic. A clause is a line or
   bullet carrying a normative keyword, with the section it came from.
2. **Propose** a verdict from the clause's own wording. Deterministic, and explicitly a
   *proposal*: violation language → `finding`, satisfaction language → `none`, undeterminable
   → `inconclusive`. No signal, or conflicting signals, → skipped rather than guessed.
3. **Draft** the case's inputs through the LLM seam (`nyx.evals.generate`), and **review** the
   pair (`nyx.evals.review`). The model may draft; the verdict is not a field it is offered.
   A human approves or rejects, and only approval makes a case scorable.

The properties this buys, and the trade each one costs:

- **The label is not the model's.** The draft schema has no verdict field, so a model cannot
  supply one even by accident. Cost: the deterministic rule is a weak classifier — run against
  `docs/SPEC.md` it proposes 17 `finding`, 0 `none`, 0 `inconclusive`.
- **A draft is never scored.** `scorable_cases()` is the single function that defines the
  golden set. Cost: nothing, and it is what stops a plausible-looking draft from becoming
  evidence.
- **The reviewer is required and never defaulted.** Cost: a review cannot be recorded
  anonymously, so an unattended process cannot grow the golden set.
- **The log is append-only and replayed.** A case proposed twice is refused, because
  re-proposing would silently revert an approval to a draft. Cost: a re-run must pick fresh
  ids rather than overwrite.

## Consequences

- The golden set can be grown by a machine and remains human-gated. That is slower than full
  automation and is the point.
- The labelling rule needs its own review. Its bias toward `finding` is a property of the
  specification's prose, not a bug to fix in the rule.
- `scripts/eval_llm_critic.py` learned to read a case file with or without a
  `fixture_response`, and to report `not_run` for the model leg rather than inventing a score.
  A skipped leg is now excluded from precision and recall instead of counted as a false
  negative.
- Per-agent calibration over time, which the same brief asks for, remains **out of reach** for
  a pure critic over a read-only context. It needs a persisted history of past claims and
  their outcomes. Recorded as a gap in `docs/implementation/` and in C-008's `cannot_assess`,
  not silently omitted.

## Alternatives considered

- **Label the case with the model.** Rejected: the model would be grading text it produced,
  which is the failure this ADR exists to prevent, and it would be invisible because the model
  is schema-validated and appears trustworthy.
- **Derive cases only from recorded historical defects.** Sound, and the eight existing cases
  are exactly that. Rejected as the *only* source: it cannot cover requirements nobody has
  broken yet, which is most of a specification.
- **Score drafts before review.** Rejected outright: a draft that passes is a number about a
  case nobody checked, and the number would be indistinguishable from a real one.
