# 0007. Authentication modes and capability enforcement

**Status:** Accepted (Phase 4, 21 October 2026).

## Context

Before Phase 4, `auth_mode` and `auth_api_key` were read only by configuration
validation and one `nyx doctor` line. Nothing authenticated an HTTP caller, and the
capability checker bound to the application was always `DenyAllCapabilityChecker`.
Evidence reads were therefore ungated: any caller that could reach the API could read
an item regardless of its sensitivity. SPEC §12 requires authentication and
capability-based authorisation; scenario 13 requires that a caller cannot read
restricted evidence without the required capability.

Two constraints shape the decision:

1. The repository's entire local workflow — the test suite, `nyx doctor`, and
   `scripts/probe_live_api.py` — calls the API with no credential. Breaking that would
   be a regression, not a hardening.
2. `config.py` already refuses to bind a non-loopback host with `auth_mode='none'`.
   A loopback bind is a local trust boundary, not a network-accessible one.

## Decision

- **`auth_mode='none'` means "local operator".** It is reachable only on a loopback
  bind, which `config.py` enforces at startup. A request without a credential in this
  mode is the local operator and receives the deployment-owner capability set. This is
  the documented trust decision that keeps every existing local caller and the probe
  working. It is not a claim that loopback callers are authenticated.
- **`auth_mode='api_key'` requires a credential on every request.** The credential is
  accepted from `Authorization: Bearer <key>` or `X-API-Key`. A missing or unknown
  credential is `401 UNAUTHENTICATED`; it never degrades to the operator identity.
  A known principal without the required capability is `403 FORBIDDEN`, or
  `403 EVIDENCE_ACCESS_DENIED` for an evidence read.
- **Principals come from configuration.** `NYX_AUTH_PRINCIPALS` names principals and
  the environment variable holding each key; `NYX_AUTH_API_KEY` is the
  single-operator case and receives the full capability set. Capability names are
  validated against the `Capability` enum when the store is loaded; an unknown name
  is a startup error naming the principal and the value. A principal whose named
  environment variable is unset is not loaded, and a warning names only the
  principal and the variable.
- **Secrets come from the environment only.** The store holds a SHA-256 hash of each
  key, never the raw key, and comparison is constant-time. No key, hash, or
  credential is logged or rendered in effective configuration.
- **Evidence access is capability-based and independent of finding access.**
  `EVIDENCE_READ` gates reading evidence. A `restricted` item — by sensitivity or by
  the `evidence:restricted` access policy — additionally requires
  `EVIDENCE_RESTRICTED`. A finding response never inlines restricted evidence
  content; for a caller without `EVIDENCE_RESTRICTED` it tombstones the omitted
  payload while keeping the evidence ID and its classification.

## Documented boundary: the CLI is not HTTP-authenticated

The CLI opens the database directly. It does not pass through the HTTP authentication
dependency, and it is **not** covered by these controls. A local user with filesystem
access to the database has the access that filesystem permissions grant — no more and
no less. The CLI must not be described as authenticated, and HTTP capability checks
must not be assumed to constrain a direct database caller.

## Consequences

- Local development, the existing test suite, and the live probe continue to work
  without credentials while still exercising the real capability path.
- Remote exposure requires an explicit `auth_mode` and at least one configured
  principal; misconfiguration fails at startup rather than at the first request.
- The deployment-owner identity in `none` mode is broad by design, which is why
  `none` mode is restricted to a loopback bind. Exposing it on a network interface
  is a configuration error, not a supported deployment.
- The CLI remains a trusted-local-operator surface; its boundary is filesystem access
  to the database and must be documented wherever the API's auth guarantees are
  stated.
- Capability names are a shared vocabulary between the HTTP layer and the lifecycle
  machine; adding a capability requires updating the enum, not a stringly-typed
  configuration convention.

## Alternatives considered

- **Require a credential even in local mode.** Rejected: it would break the documented
  local workflow and the probe, and a loopback bind is already the trust boundary.
- **Treat an invalid key as anonymous.** Rejected: that silently converts a failed
  authentication into a privileged identity, the most dangerous possible fallback.
- **Store raw API keys in configuration.** Rejected: configuration is dumped by
  diagnostics and could be logged; only a hash is kept.
- **Gate evidence reads only on sensitivity, ignoring `access_policy_ref`.** Rejected:
  the policy reference is the field that declares the required capability, and
  ignoring it would silently downgrade a restricted policy to a plain read.
- **Let findings inline their evidence payloads.** Rejected: SPEC §9.2 and
  `docs/privacy-and-retention.md` §6 require evidence access to be independent of
  finding access.
