# NYX v0.0.2 — changelog

## What changed

- **Auth:** central protected-router authentication plus explicit per-operation capabilities.
  Health and readiness stay exempt; every other route is default-deny.
- **Derived outputs:** one fail-closed authorisation projection follows evidence sensitivity and
  policy references across finding detail, finding list, history, and critic-run responses.
- **Citations and attribution:** a disposition's references are validated before any transition;
  producer-claimed verification is stored separately from trusted submitter metadata and never
  upgraded.
- **Completion support:** a completion claim's covering probe observations are assessed by their
  outcome — `not_observed`, `error`, and `skipped` no longer clear positive support, and an
  undated or wrong-version observation cannot verify a dated claim. This landed as observation
  outcomes and verification strength rather than the structured CI fields the task description
  named; the evidence model still has no structured test-outcome field, and C-001's evidence path
  still reads outcomes from text markers.
- **Audit chain:** checkpoint comparison recomputes the chain and the anchor from one consistent
  read, so a body-only mutation or an interior deletion fails where trusting stored hash labels
  would have passed.
- **Model critic:** the assessment target is carried inside a bounded, canonical untrusted block;
  the prompt template version was bumped.
- **Durable work:** an assessment queue with lease, retry, and visible terminal failure.
  Transactionally scheduled with ingestion, drained by an explicit caller.
- **Agent transcripts:** a pending call is distinct from a completed result; only completed
  immutable results become result evidence.
- **Probe budgets:** cancellable owned-process isolation on Windows with a shared batch deadline,
  a typed refusal for unpicklable callbacks, and no abandoned workers.
- **Core without extras:** the core CLI and API work when the optional conversational extra is
  absent; the conversational commands report a useful missing-extra error.

## New operational surface

- `nyx assessment backlog --json` — queued, leased, and dead-lettered assessment work.
- `nyx assessment drain --json` — run queued work now. NYX never starts a background worker; the
  queue is written on ingest and drained only when a caller asks.

## Evaluation

- `evals/v0.0.2-corpus/cases.jsonl` — 132 frozen, labelled scenarios across positive, negative,
  unknown, and adversarial categories, covering all ten reproduced audit gaps. Labels were
  written from the SPEC and the critic contracts before the implementation was consulted; every
  disagreement and its resolution is recorded in the corpus README.
- `scripts/eval_v002_corpus.py` — scores the deterministic pack against the corpus, per category,
  under a chosen flag configuration.
- `scripts/v002_acceptance_harness.py` — drives the request-level cases and the restart journey
  against a real service on an owned loopback port: authentication, derived-output masking,
  citation validation, audit-body mutation detection, durable work across a restart, and the
  claim-to-disposition path.
- `scripts/v002_outage_probe.py` — an owned loopback relay induces an outage, then recovers;
  verifies bounded failure, a recorded failure, recovery, and durability after reopening the
  store.

## Known limitations

- **No live model acceptance.** The remote NYX deployment has no configured model or key, and
  this release does not claim a live model run. The outage probe exercises the transport against
  an owned relay; it is not a model-quality measurement.
- **C-006's stronger rule is not wired.** `require_valid_authorisation` exists, but the
  authorisation lookup it needs is not bound in the service, so a reference that does not
  authorise the action it cites is not yet detected in production.
- **The deterministic outcome reader is text-based.** C-001's evidence path reads pass/fail
  markers from titles and descriptions, so injected marker words change the verdict. The
  injection defence is on the model path; this is a limitation of the evidence model, recorded in
  the corpus (`v002-adv-02`).
- **C-012's documentation overstates RULE 1.** The module docstring says an unrecognised verb is
  not a scope violation; RULE 1 compares declared targets against the allow-list regardless of
  verb. Recorded in the corpus (`v002-c012-04`).
- **The container image base still carries eight Debian CVEs** with no fix in trixie. They are
  listed in `.trivyignore` with the re-check condition; see `docs/operations.md`.

## Compatibility

- Two additive migrations: `d6e7f8a9b0c1` (evidence provenance claims) and `v002_assessment`
  (durable assessment work). Both are additive; no column is dropped and no row is rewritten.
  `alembic upgrade head` reaches a single head.
- `auth_mode='api_key'` now requires a key on every protected route. A deployment relying on the
  previous anonymous access must set `NYX_AUTH_API_KEY` or `NYX_AUTH_PRINCIPALS`.
- The version is 0.0.2 across `pyproject.toml`, `uv.lock`, `nyx.__version__`, and the generated
  OpenAPI document.