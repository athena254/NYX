# 0005. Evidence is not proof

**Status:** Accepted (Phase 0, 19 October 2026).

## Context

NYX must decide whether a completion claim, a research conclusion, a memory, or a routing report is
supported. The tempting design is to compute one confidence score per claim and compare it to a
threshold. That design destroys the information a reviewer needs: where a record came from, what was
verified, what is stale, which source is merely repeating another, and what remains unknown. A hash or
signature is often mistaken for proof of truth, and a model's confidence is often mistaken for a
calibrated probability.

## Decision

- NYX keeps **evidence references, provenance, scope, freshness, integrity method, and limitations**
  as first-class fields.
- Every finding separates **observation**, **inference**, **recommendation**, and **unknowns**.
- `confidence` is optional, carries its **method**, and is never a substitute for evidence or a gate
  for confirmation.
- A hash or signature is described as an **integrity identifier** or an authenticity mechanism under
  stated assumptions, never as proof that the source statement is true.
- Model output is **assessment**, labelled with model/provider version and prompt-template version,
  and never labelled deterministic verification.
- Missing telemetry is represented as **unknown** or **incomplete**, not as misconduct.
- Disposition uses the weakest conclusion supported by the evidence; `inconclusive` is a valid and
  common outcome.

## Consequences

- Findings are larger and more structured than a score, but they are inspectable and appealable.
- Downstream consumers cannot compare findings by a single number; they must read scope and
  limitations. That is intentional.
- The data model needs explicit fields for counterevidence, limitations, unknowns, and critic
  versions, which Phase 1 schemas already include.
- Calibration, if ever needed, is a separate, documented exercise with an evaluation corpus, not an
  assumption about model confidence.

## Alternatives considered

- **Single confidence score from a model.** Rejected: hides provenance, ignores scope and staleness,
  and is not calibrated.
- **Boolean "verified" flag.** Rejected: loses the distinction between a reproducible check, a signed
  attestation, a trace, and a model opinion.
- **Trust the strongest-looking source.** Rejected: relevance and scope are proposition-specific; a
  strong source can be irrelevant to the claim at hand.
- **Treat integrity as truth.** Rejected: a correctly signed but false statement is still false. This
  is the central error the SPEC warns against.
