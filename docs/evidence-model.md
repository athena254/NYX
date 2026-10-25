# NYX evidence model

**Status:** Phase 0 baseline. The formal machine-readable shape is in
`schemas/evidence.schema.json`; the Pydantic model and its agreement test are Phase 1 work.

## 1. Core rule

> **A hash or signature is an integrity identifier, not proof of truth.**

A digest can show that stored bytes match previously recorded bytes. A signature can show that a
private key holder signed a record, subject to key custody and trust assumptions. An attestation can
show that a described step occurred under a stated process. None of these establishes by itself that a
sentence in the record is true, complete, relevant to the claim, or current.

NYX therefore preserves the evidence reference, source, scope, time, integrity result, and limitations.
It does not collapse them into a single “truth score”. The optional `confidence` field is an assessment
aid with a stated method, never a replacement for evidence.

## 2. Evidence categories (SPEC §6)

These are NYX conventions, not a universal external standard. A category describes **how** a
proposition is supported, not whether the proposition is ultimately true.

| Category | What it can establish | What it cannot establish |
| --- | --- | --- |
| **Deterministically verified** | A reproducible test, policy evaluation, signature verification, or well-defined check passed or failed for stated conditions. | That the check covers all conditions, that the environment matches production, or that the tested artifact is the deployed artifact. |
| **Directly observed** | An authenticated execution, CI, source, or infrastructure record exists and says what it says. | That the observation is complete, that unrecorded events did not occur, or that the producer is honest. |
| **Corroborated** | Multiple sources support the same conclusion. | Independence, unless lineage shows they do not merely repeat one original claim. |
| **Inferred / hypothetical** | A model or heuristic raises a plausible concern that warrants further checks. | A fact, a deterministic result, or a final verdict. |

Every assessment should record: source authenticity and integrity; observation time and staleness;
scope of the test or observation; evaluator/model/rule version; reproducibility details; known blind
spots; counterevidence; and a confidence rationale, not only a number.

## 3. Evidence hierarchy (SPEC §7.1)

Usually prefer, where relevant and authentic:

1. **Reproducible deterministic test or measurement** with captured inputs and environment.
2. **Direct source/artifact inspection** tied to an immutable commit or content hash.
3. **Independently verifiable signed build/release attestation.**
4. **Traceable runtime logs or traces** with known instrumentation coverage.
5. **Independent expert analysis** with an explicit method.
6. **Model-generated analysis.**
7. **Unattributed assertion.**

This is guidance, not an automatic truth ranking. Evidence must be evaluated for the specific
proposition:

- A unit test cannot by itself prove that a production deployment used the tested artifact.
- A signed attestation cannot prove that the attested process was sound.
- A trace cannot prove the absence of uninstrumented actions.
- A model analysis cannot be promoted to deterministic verification by repetition or confidence.
- A later summary of an earlier report is not a second independent source.

## 4. `integrity_method` values

`integrity_method` records what mechanism was applied to the evidence record or its referenced content.
It is distinct from **relevance**, **scope**, **freshness**, and **truth**.

| Value | What it does establish | What it does not establish |
| --- | --- | --- |
| **`none`** | Nothing about integrity. The record is a reference or assertion whose content has not been checked. | That the content is unchanged, authentic, complete, or true. |
| **`hash`** | The stored or canonicalised content matches a recorded digest at the time of checking; a mismatch reveals a change in the checked bytes. | Who produced the content, that the content is true, that the digest itself is trustworthy, or that the content is current. A hash may be sensitive because it enables correlation or guessing. |
| **`signed`** | A signature over the record validates under a stated key and algorithm, so the holder of the signing key produced or endorsed the signed bytes. | That the signer is honest, authorised, or correct; that the key was not compromised; that the signed statement is true; that the signature covers the relevant scope. |
| **`attested`** | A structured attestation links an actor, step, input, and output under a defined process, and its own verification result is recorded. | That every process step was followed, that inputs were trustworthy, that the attestation is complete, or that the output is correct. |
| **`external`** | NYX is relying on an external verification mechanism (for example a transparency log or an external verifier) and records the mechanism and its result. | That the external mechanism is honest, available, correctly configured, or sufficient for the claim. The external trust assumption must be documented. |

`integrity_method` never upgrades a record to `confirmed`. A `hash` result is an integrity result, not
a truth result. A `signed` record is not “more true” than an unsigned record; it has a different
authenticity assumption.

## 5. Evidence record fields and their purpose

| Field | Purpose | Common mistake it prevents |
| --- | --- | --- |
| `id`, `schema_version` | Stable identity and versioned shape. | Losing which interpretation of a record applied. |
| `kind` | Coarse type (test result, log, trace, attestation, measurement, etc.). | Treating unlike evidence as interchangeable. |
| `title`, `description` | Human-readable summary separate from source content. | Hiding the claim inside a blob. |
| `source_system_id` / `source_name` | Where the record came from. | Unattributed assertions. |
| `source_ref`, `locator` | Exact URI/path/commit/trace and line range, JSON pointer, test ID, or span. | “Somewhere in CI” evidence. |
| `captured_at`, `valid_from`, `valid_until` | When the record was captured and the window in which it is valid for the claim. | Using an old passing run to verify a new commit. |
| `content_hash` | Integrity identifier for stored/canonicalised content where feasible. | Silent content changes. |
| `integrity_method`, `verification` | What was checked, how, by whom, and the result. | Confusing “signed” with “true”. |
| `producer_identity` | Who asserted or produced the record. | Treating an assertion as self-validating. |
| `relevance_to_claim` / `relevance_to_finding` | Which proposition the item is offered for. | Collecting evidence that is authentic but irrelevant. |
| `limitations` | Known blind spots, assumptions, and gaps. | Overstating what the item proves. |
| `sensitivity` | `public`, `internal`, `confidential`, or `restricted`. | Leaking restricted content through a finding. |
| `access_policy_ref` | Which capability is required to read it. | Treating finding access as evidence access. |
| `retention_class` | How long the record and payload may be kept. | Keeping sensitive data indefinitely by accident. |

## 6. Independence and corroboration

Corroboration counts only when the sources are genuinely independent. NYX records lineage and
dependence so that:

- a CI summary derived from a test log is not counted as a second source;
- two agents repeating the same instruction are not two witnesses;
- a model summary of a document is not independent of the document;
- a later re-publication of a claim is not new evidence for the claim.

When independence cannot be established, the assessment says so and reduces the weight accordingly.
It does not silently count documents.

## 7. Freshness and scope

An evidence item is usable for a claim only when its declared scope and validity window cover the
claim. The minimum checks are:

- same system and component;
- same artifact, commit/build, version, or explicit compatibility statement;
- same environment or a documented equivalence;
- observation time within the claim's relevant period;
- no later artifact change that invalidates the observation.

A stale or mismatched item is evidence **about the older scope**, not proof about the current scope.
The correct result is a scoped verification gap, a freshness finding, or `inconclusive` — not an
accusation that the code is broken.

## 8. Counterevidence

The evidence model requires a place for counterevidence even when the current finding is strong.
Counterevidence can:

- narrow the scope of an inference;
- show an alternative explanation;
- show a later fix;
- show that the source was compromised or the check was misconfigured;
- overturn the finding entirely.

Counterevidence appends to history. It never deletes the original evidence or finding. A disposition
that relies on it records which premise failed or which counterevidence prevailed.

## 9. Evidence packet

A serious criticism produces a structured, versioned packet containing, at minimum:

- finding identity and target;
- the exact claim and its scope;
- the expectation, policy ID, policy version, and acceptance criteria;
- evidence references with source, digest, observation time, integrity result, and relevance;
- reasoning that separates observation from inference;
- limitations and unknowns;
- counterevidence;
- response deadline, if any;
- disposition state and supersession link.

The illustrative packet in SPEC §5 is a shape guide. The persisted finding schema in
`schemas/finding.schema.json` is the Phase 0 contract; Phase 1 must keep the Pydantic model and the
schema in agreement.

## 10. What NYX will not do with evidence

- It will not convert an integrity check into a truth claim.
- It will not treat a missing record as misconduct.
- It will not treat repeated summaries as independent corroboration.
- It will not let the monitored system write to the evidence it is judged against.
- It will not copy restricted evidence into a finding merely because the caller can read the finding.
- It will not send unredacted sensitive payloads to a model.
- It will not use model confidence as a calibrated probability without separate calibration work.

---

## 11. Four kinds of evidence: claim, observation, artifact, authorisation

Everything above describes **what a producer reported**. The ground-truth layer adds the other
three kinds, and the separation between them is the point rather than bookkeeping.

| Kind | Record | Answers | Does not answer |
| --- | --- | --- | --- |
| **Claim** | `nyx.domain.claims.Claim` | What an agent or system **asserts**: done, fixed, deployed, tests pass. | Whether it is true. |
| **Observation** | `nyx.domain.observation.Observation` | What **NYX itself gathered** through a probe about the real end state. | Whether the probe covered everything; an observation is one look, not a survey. |
| **Artifact** | `EvidenceItem` with `kind=artifact_digest` and a `content_hash` | That a stored file or bundle is byte-identical to a previously recorded digest. | That its content is true (ADR 0005). |
| **Authorisation** | `nyx.domain.authorisation.Authorisation` | Who approved what action, for what scope, until when. | That the approval was wise, or that the action was taken. |

> **A transcript is what an agent says and does. An outcome is the actual final state of the
> environment.** Claiming a booking was made is not the same as the booking existing in the
> database. Keeping the two in different records is what makes the second checkable independently
> of the first.

Two consequences follow, and both are enforced in code rather than documented only:

1. **A claimant cannot verify itself.** `nyx.evidence.verification.strength_of_evidence` returns
   `self_reported` for *every* evidence item, whatever its `integrity_method`. A signed attestation
   is still a claim about the world made by someone.
2. **An observation carries its own strength, decided by the probe kind that produced it.**
   `strength_of_observation` is a pass-through, so a probe can say what it is worth without a
   second place needing to know how it works.

### 11.1 Verification strength

One shared enum, `VerificationStrength`, ordered weakest to strongest:

| Strength | Meaning | Example |
| --- | --- | --- |
| `none` | Nothing backs the proposition. An agent said it; that is all. | A transcript sentence with no linked record. |
| `self_reported` | The claimant's own record is the only support. | A CI record the claimant produced. |
| `rerun_by_nyx` | NYX re-executed the check itself at the current subject version. | `ReexecutionProbe` returning exit code 0. |
| `independently_observed_end_state` | NYX observed the real end state of the environment. | `StateCheckProbe` reading the live row; `SideEffectProbe` finding the effect. |

The ordering is what makes "is this strong enough?" a comparison rather than a judgement:
`nyx.evidence.verification.is_at_least` is an index test. The floor for a completion claim is
`COMPLETION_MINIMUM_STRENGTH = rerun_by_nyx`; a claim backed only by `self_reported` evidence does
not clear it, and the upgraded C-001 says so.

An empty support set is `strongest([]) == none`, not an error. "Nothing backs this" is a real state
a critic must be able to see.

### 11.2 What an observation establishes, and what it does not

An observation is **one probe run against one subject at one version**. It does not establish:

- that the probe covered the whole subject (a probe that reads one row is not a survey);
- that the subject is the deployed artifact (a staging observation is not production);
- that nothing else changed since (a later change can invalidate it);
- that the probe's own instrumentation is complete.

It *does* establish that NYX, independently of the claimant, looked at version `subject_version` of
`subject` and found `detail`. That is exactly the gap the claim/observation split opens, and it is
the most a single probe can honestly say.

### 11.3 Observations are linked to claims, not to subjects

`Observation.relevance_to_claim` is what makes an observation *support*. An observation gathered
before any claim exists is still stored — an adapter observes before a claim references it — but it
is support for no particular proposition. `evidence_supports(claim_id, ...)` respects the link on
both sides, so a strong observation of the wrong claim cannot verify this one.

### 11.4 The subject version is the freshness anchor

`Observation.subject_version` is the commit, build hash, config hash, or deployment id the
observation applies to. A critic binds an observation to the claim through it: an observation of
commit `def456` cannot verify a claim about `abc123`. An observation with **no** declared version
does not match a dated claim at all — an undated observation cannot verify a dated one. That is the
same rule C-003 already applies to evidence, applied to the stronger record.

### 11.5 Probes are read-only by default

`Probe.read_only` is a declared property, not a discovery. A probe that reports `False` mutated
something, and `ProbeRegistry` refuses to run it unless the mutating-probe gate is open **and** an
unexpired, in-scope `Authorisation` covers the action. `Settings.probes_enabled` is the kill switch
and stops every probe, read-only included. See `docs/probe-authoring.md` and
`docs/safety-model.md`.

### 11.6 Claim extraction is deterministic and traceable

`nyx.evidence.claims.RegexClaimExtractor` turns transcript text into typed claims. A claim is the
**sentence** containing a match, not the match: the span is a unit a reader would quote, and the
certainty markers are read from that same span so "The tests passed 100%." carries its own
overconfidence. Every claim keeps `(start, end)` offsets into the original text, because a claim
whose text differs from its span cannot be audited.

Extraction is conservative in both directions: a sentence no pattern matches yields no claim, a
question yields no claim, and one sentence can yield several claims when several patterns match it.

An LLM may assist later behind the `ClaimExtractor` seam. Any implementation must keep the source
span, which is why `ExtractedClaim.span` is required rather than optional.
