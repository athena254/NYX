# NYX

**Evidence-backed critic and ecosystem oversight service.**

NYX observes a multi-agent ecosystem, detects contradictions and unsupported claims, challenges them
with inspectable evidence, accepts rebuttals, independently verifies disputed claims, and records a
reasoned, auditable disposition for every case.

The defining rule:

> No serious accusation without inspectable evidence; no high-impact conclusion without proportionate
> verification; no verdict that is immune to challenge.

NYX is a **critic**, not an unchecked ruler. It never silently modifies, disables, deploys, deletes,
or changes permissions in another system. High-impact output is a recommendation for an authorised
policy gate or human.

## Status

Functional for local and controlled deployment. The deterministic core, disputes, hardening,
integrations, and the conversational shell are complete. Phase 6's model critic is wired and
opt-in (`NYX_LLM_CRITIC_ENABLED`); its evaluation corpus exists, but the **live calibration run
and numeric thresholds are still pending**, so Phase 6 is not claimed complete. See
`docs/implementation/roadmap.md` for phase status and `docs/implementation/acceptance-tests.md`
for what is actually verified.

Nothing in this repository should be read as a claim that a phase is complete until its acceptance
scenarios pass in `uv run pytest`.

## Documents

- `docs/SPEC.md` — the research and implementation contract (authoritative).
- `docs/architecture.md` — module map and boundaries.
- `docs/threat-model.md` — trust boundaries, threats, and defenses.
- `docs/evidence-model.md` — what each evidence kind does and does not establish.
- `docs/api.md` — HTTP API contract.
- `docs/CLI-CONTRACT.md` — the CLI's machine interface, which every command implements.
- `docs/audit-verification.md` — the hash chain, its trust limits, and external checkpoints.
- `docs/adr/` — consequential design decisions.
- `AGENTS.md` — binding rules for coding agents working here.

## Demonstrations

```bash
uv run nyx seed demo          # Ghost Factory fixtures, through the ordinary ingestion gateway
uv run nyx audit checkpoint   # anchor the chain out-of-band (ADR 0012)
uv run nyx doctor --probe     # real reachability, opt-in
```

The seed is idempotent: a second run produces duplicates, not new rows.

## Quick start

```bash
uv sync
uv run alembic upgrade head
uv run nyx doctor
uv run pytest
```

## License

Apache License 2.0 — see `LICENSE`. New runtime dependencies must remain compatible with
Apache-2.0; the dependency review for each addition records its licence.
