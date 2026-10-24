# 0008. Adapter boundary and independent disablement

**Status:** Accepted (Phase 5 slice 5.0, 21 October 2026).

## Context

SPEC §14 requires integrations to be *adapters, not prerequisites for the core domain model*,
and Phase 5's exit criterion (SPEC §19) is that *adapters can be disabled independently and core
NYX still works*. Before Phase 5 there was no adapter abstraction at all: `src/nyx/adapters/`
existed as an empty, untracked directory, and `docs/architecture.md` §2 carried only a sketch of
an `Adapter` protocol with no implementation.

Three constraints shape the decision:

1. **An adapter must not become a second ingestion path.** The existing gateway already provides
   size limits, JSON decoding, schema validation, redaction before persistence, identity-based
   deduplication, and an audit entry per accepted record. A parallel path would have to
   reimplement all of it, and would drift.
2. **An adapter must not be able to judge.** ADR 0004 already establishes that adapters are
   read-only observers in the MVP. Nothing in the adapter layer may create a finding, record a
   disposition, or change a lifecycle state.
3. **Phase 4 defect #7 was a doctor check that reported *configuration* as *availability*.** Any
   "is the adapter healthy?" answer has to be honest about the difference between *registered*,
   *enabled*, and *reachable*.

## Decision

- **An adapter is a producer behind the ingestion gateway.** `Adapter.normalize()` returns
  producer *input* models (`EventInput`, `ClaimInput`, `EvidenceInput`) — never domain models.
  Domain models would skip redaction and schema validation, which is the one thing the boundary
  guarantees. `src/nyx/adapters/base.py` → `nyx.ingestion.inputs` → `IngestionGateway`.
- **`Adapter` is a `Protocol`, not a base class.** An adapter is a class with the right attributes
  and methods and nothing else: no storage, no session, no `Session` import. Nothing in `domain`
  knows adapters exist.
- **The registry holds the code inventory; `Settings.enabled_adapters` holds the deployment
  filter.** `AdapterRegistry.registered()` lists what exists; `enabled()` applies the filter.
  Keeping them apart is what lets `nyx doctor` say `registered: git-ci; enabled: false` without
  lying.
- **"Disabled" means the adapter is not constructed — and therefore not probed.**
  `AdapterRegistry.states()` probes *only enabled* adapters. A constructed-but-ignored adapter
  still has a `health()` method that can be called, and a health method that fetches can hang.
  Disabling must remove the object, not skip a call.
- **Enabling an adapter requires configuration that can actually work.** `enabled_adapters` naming
  `git-ci` with an empty `git_ci_repository`, or with the `github` provider and no
  `git_ci_token_env`, fails at startup naming the variable. These are misconfigurations, not
  degraded states: an adapter with no target repository has nothing to observe.
- **The token is never a setting.** `git_ci_token_env` names the environment variable that holds
  it; the value is read once at client construction. A token therefore never enters effective
  configuration, a log line, or a health detail.
- **`git_ci_allow_private_targets` must agree with `evidence_allow_private_urls`.** Two switches
  that disagree about which targets are fetchable would let an adapter and the SSRF guard reach
  different answers about the same URL.
- **`ADAPTER_DISABLED` and `ADAPTER_UNAVAILABLE` are distinct codes** (409 non-retryable, 503
  retryable). `DEPENDENCY_UNAVAILABLE` is deliberately *not* reused: an optional adapter being off
  is not a required dependency being down, and conflating them is how a disabled adapter gets
  reported as an outage.
- **The local-git provider reads `.git` with the standard library, not `subprocess`.**
  `tests/security/test_verification_allowlist.py` fails the build on `subprocess`, `os.system`,
  `shell=True`, `eval(`, or `exec(` anywhere in `src/`. A reviewed allowlist entry for a shell-out
  is a weaker guarantee than not having the shell-out, so the provider reads `.git/HEAD`,
  `.git/refs/**`, `.git/packed-refs`, and `.git/logs/HEAD` directly. The original limitation —
  refs and reflog only, no commit contents, file lists, or diffs — was lifted on 24 October 2026
  by ADR 0011, which adds loose and packed object reading with the standard library and still
  starts no process. Textual diffs remain out of scope.

## Unlinked evidence (SPEC §5.4 deviation)

SPEC §5.4 lists `relevance_to_claim` or `relevance_to_finding` as a required evidence field.
SPEC §14 requires a Git/CI adapter that observes a CI run. Those two cannot both hold
literally: an adapter fetches a run *before* any claim references it, so at registration the
evidence has no claim to point at.

Emitting a claim from the same CI run was considered and rejected — it would make C-001
assess a claim against evidence from its own source, which SPEC §8 explicitly warns is not
independent corroboration.

**Resolution:** `EvidenceItem._require_source_and_relevance` now accepts a third state —
neither relevance target, but a `relevance_note` explaining why. The original refusal is
intact for a producer that simply omits the field; unlinked evidence must *say it is
unlinked*. No column changed, no schema changed, and the adapter states the reason in the
record rather than leaving a reader to infer it.

This was found by a probe written outside the project's test helpers
(`tmp/probes/probe_git_ci_adapter.py`), not by slice 5.2's 20 unit tests: those asserted that
`normalize()` returns valid Pydantic input models, and never pushed the result through the
real ingestion path against a migrated database. **A module's tests were written by whoever
wrote the module** — the probe is what caught it.

## Consequences

- An adapter inherits redaction, validation, deduplication, and audit for free, and a new adapter
  cannot accidentally bypass them.
- The boundary is executable, not documentary: gate G7 (`tests/contract/test_adapter_boundary.py`)
  fails the build if a module under `src/nyx/adapters/` imports from the deterministic core, or if a
  core module imports `nyx.adapters` outside the two orchestrator files.
- Disabling an adapter is a one-line configuration change with no code edit and no cleanup: the
  adapter is not constructed, and the records it already wrote remain valid, inspectable, and
  auditable. Turning an adapter off stops new observations; it does not retract old ones.
- The registry and the deployment filter are separate objects, so a future deployment can list
  adapters in configuration without the registry having to know what configuration is.
- The local-git provider is the weakest of the three providers by design, and says so in its own
  docstring rather than in a ticket nobody reads.

## Alternatives considered

- **An abstract base class for adapters.** Rejected: it would make `domain` aware of the adapter
  layer, and inheritance invites an adapter to reach for state it should not have.
- **`normalize()` returning domain models.** Rejected: it would skip redaction and schema
  validation, which is the entire reason the boundary exists.
- **A constructed-but-ignored adapter.** Rejected: a constructed adapter has a `health()` that can
  be called, and a health method that fetches can hang a request. Disabling must remove the object.
- **Reusing `DEPENDENCY_UNAVAILABLE` for a disabled adapter.** Rejected: it would report an
  optional adapter being off as a service outage.
- **Storing the Git/CI token as a `SecretStr` setting.** Rejected: effective configuration is
  rendered by diagnostics, and a named environment variable keeps the value out of it entirely.
- **Letting the local-git provider shell out to `git` under a reviewed allowlist entry.**
  Rejected: an allowlist is a reviewed exception, and the standard library can read the refs and
  reflog this adapter needs. The weaker guarantee was not worth the simpler implementation.
- **Putting the adapter behind the HTTP API instead of the ingestion gateway.** Rejected: it would
  create a second ingestion path and a second place for redaction and validation to be forgotten.
