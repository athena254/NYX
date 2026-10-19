# NYX privacy and retention

**Status:** Phase 0 policy baseline. Enforcement is Phase 4 work; Phase 1 must already redact before
persistence and carry the classification and retention fields on every record.
**Contract:** `docs/SPEC.md` §11.3 and §12; ADR 0003 and ADR 0006.

## 1. Principles

1. **Minimise collection.** NYX collects what is needed to assess a claim, not full transcripts.
2. **Redact before persistence and before model calls.** A secret that reaches the database or a model
   prompt has already leaked.
3. **Classify every record.** Sensitivity is explicit, not inferred at read time.
4. **Access is capability-based.** Finding access does not imply evidence access.
5. **A hash can itself be sensitive.** A digest can enable correlation, dictionary guessing, or
   confirmation of a guessed value, so integrity metadata is in scope for confidentiality.
6. **Retention is bounded.** Records are kept for a class-specific period, not forever.
7. **Redaction is auditable.** The fact of a change is recorded where lawful, even when the content is
   gone.
8. **No promise of immutability.** An administrator who can rewrite the database can defeat local
   integrity controls. Retention controls are operational, not cryptographic guarantees.

## 2. Data classification

| Class | Definition | Examples | Required handling |
| --- | --- | --- | --- |
| `public` | Deliberately publishable; disclosure causes no harm. | Public repository URLs, public commit IDs, published policy IDs. | Normal storage; may be logged. |
| `internal` | Operational data not intended for external disclosure. | Event metadata, source references, findings about internal systems, most evidence references. | Stored and logged at metadata level; no public export without approval. |
| `confidential` | Sensitive operational or commercial data. | Internal logs, unreleased code pointers, security findings before disclosure, internal review records. | Redaction of sensitive values; capability-gated access; no logging of content. |
| `restricted` | Data whose exposure would cause material harm or legal exposure. | Credentials, tokens, private keys, personal data, security-sensitive exploit detail, protected evidence. | Never in ordinary traces or case summaries; explicit capability; minimal retention; redaction by default. |

Rules:

- Every event, evidence item, finding, and audit entry carries a classification derived from the
  evidence schema's `sensitivity` field.
- A record inherits the highest classification of its content and referenced items.
- A `restricted` item must not be copied into a finding payload; the finding references it by ID and
  access policy.
- Classification cannot be silently downgraded. A downgrade is an audited action with an actor and a
  reason.

## 3. Redaction before persistence

Redaction runs in `ingestion` and `evidence` **before** the record is written and **before** any
content is sent to a model or external adapter.

- A detector recognises high-confidence secret patterns (for example provider key prefixes, private
  key headers, bearer tokens, password assignments, connection strings with credentials).
- The redaction result replaces the sensitive value with a typed placeholder such as
  `[REDACTED:provider_api_key]`. The original value is not stored in NYX.
- Redaction failure **fails closed**: `REDACTION_FAILED`, no write, no model call.
- Redaction never rewrites a hash to hide a detection; a hash that is itself sensitive is removed or
  replaced under the deletion workflow below.
- Model prompts and adapter payloads state that content is untrusted (ADR 0006) and receive only
  redacted content.
- Logs and traces use identifiers and classifications, not payloads. Metric labels never contain user
  IDs, full claim text, secrets, or unbounded values.

Known limitation: pattern-based redaction cannot catch every secret, especially novel formats or
secrets split across fields. Structural controls (do not accept raw credentials; require vault
references) and least-privilege access are the primary defences, not the detector.

## 4. Retention classes

Exact durations are deployment configuration; Phase 0 fixes the classes and their intent, not a
number, because obligations differ by deployment. A deployment must record its chosen durations in its
configuration and must not silently exceed them.

| Class | Intended use | Retention intent | Notes |
| --- | --- | --- | --- |
| `ephemeral` | Raw payloads, volatile observations, debugging captures. | Shortest period; may be hours to days. | First candidate for redaction or deletion. Metadata may outlive the payload. |
| `standard` | Normal event, claim, and evidence metadata. | Operational default. | Sufficient to reconstruct the workflow while it is relevant. |
| `extended` | Findings, rebuttals, dispositions, and their evidence references. | Long enough to support appeal, audit, and regression analysis. | Append-only history; payload content may still be redacted. |
| `legal_hold` | Records subject to an investigation or legal obligation. | Kept until the hold is released by an authorised principal. | Release is an audited action; cannot be set by an event payload. |
| `derived_metadata` | Hashes, counts, coverage notes, and lineage with no payload. | May be kept longer than payloads. | Still subject to the hash-sensitivity rule. |

Metadata and hashes are kept only where policy allows. A hash tied to a `confidential` or `restricted`
payload is itself treated as at least that sensitive.

## 5. Deletion and redaction workflow (SPEC §11.3)

A deletion or redaction request follows this workflow. It is available only to a principal with the
retention capability.

1. **Request.** Record the requester, the target record or scope, the requested action (delete payload,
   redact fields, remove hash), and the legal or operational reason.
2. **Authorise.** Verify the requester's capability and the obligation. If the record is under
   `legal_hold`, deny and explain.
3. **Locate.** Identify every copy: primary row, inline payload, derived summaries, search/index
   entries, cached findings, adapter copies, and any model-prompt retention. Derived copies must be
   handled, not just the primary row.
4. **Apply.** Replace payload content with a tombstone or a typed placeholder. Remove the hash where
   the hash is itself sensitive. Do not delete the record's identity or its relationships unless the
   obligation requires it.
5. **Preserve the fact.** Append an audit entry recording actor, target, action, reason, time,
   extent, and whether payload or hash was removed. Where recording the fact is not lawful, the
   deployment documents that exception before using it.
6. **Invalidate caches.** Invalidate search indexes, caches, and any derived material that still
   contains the removed content.
7. **Verify.** Re-read the primary record and each known derived copy and confirm the content is
   absent. Record the verification result.
8. **Notify.** Return the outcome and the audit reference to the requester.

Constraints:

- Redaction supersedes content but does not rewrite findings, rebuttals, or dispositions; their history
  and relationships remain inspectable.
- A disposition that relied on redacted evidence records that the evidence was redacted and remains
  contestable.
- Deletion of the underlying database by an administrator is outside NYX's control and is not
  presented as prevented.
- Redaction does not remove the append-only audit entry that records the redaction, unless a lawful
  exception is documented.

## 6. Access control

- Every evidence item carries an `access_policy_ref` and a classification.
- Reading evidence requires the capability named by its access policy, independent of whether the
  caller can read the finding that references it.
- A finding response omits or redacts restricted evidence it references when the caller lacks the
  evidence capability.
- Bulk export requires an explicit export capability and is audited.
- Authentication is required for non-local network use; the default bind is local.
- Adapters receive the least privilege needed for their read-only role and never a credential that
  grants write or administrative access to a monitored system.

## 7. Model and adapter data handling

- No model call is made with unredacted `confidential` or `restricted` content.
- Provider, model, prompt-template version, and privacy mode are recorded with each model critic run.
- Prompt/response storage follows this policy; by default, content is not persisted beyond the
  assessment it produced.
- External adapters are disabled by default and subject to a separate privacy review before they can
  send data outside the deployment.
- The no-LLM mode is fully functional; no provider key is required or hard-coded.

## 8. Residual risks

- Pattern redaction is incomplete; a novel secret format can pass through.
- A hash can leak information through correlation or a dictionary attack; it is not automatically
  safer than the value.
- Derived summaries created before a redaction request may retain content if step 3 missed a copy;
  the workflow's verification step exists to catch this, but completeness depends on inventory
  accuracy.
- Backups and filesystem snapshots are governed by the host, not by NYX.
- Legal-hold and lawful-exception decisions are deployment-specific and require human authority.
