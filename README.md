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

Under construction. See `docs/implementation/roadmap.md` for phase status and
`docs/implementation/acceptance-tests.md` for what is actually verified.

Nothing in this repository should be read as a claim that a phase is complete until its acceptance
scenarios pass in `uv run pytest`.

## Documents

- `docs/SPEC.md` — the research and implementation contract (authoritative).
- `docs/architecture.md` — module map and boundaries.
- `docs/threat-model.md` — trust boundaries, threats, and defenses.
- `docs/evidence-model.md` — what each evidence kind does and does not establish.
- `docs/api.md` — HTTP API contract.
- `docs/adr/` — consequential design decisions.
- `AGENTS.md` — binding rules for coding agents working here.

## Quick start

```bash
uv sync
uv run alembic upgrade head
uv run nyx doctor
uv run pytest
```

## License

Not yet chosen — decide before any external distribution.
