# NYX

Evidence-backed critic and ecosystem oversight service. NYX watches a multi-agent ecosystem, finds contradictions and unsupported claims, challenges them with inspectable evidence, accepts rebuttals, independently verifies disputed claims, and records a reasoned, auditable disposition for every case.

The defining rule:

> No serious accusation without inspectable evidence; no high-impact conclusion without proportionate verification; no verdict that is immune to challenge.

NYX is a critic, not an unchecked ruler. It never silently modifies, disables, deploys, deletes, or changes permissions in another system. High-impact output is a recommendation for an authorised policy gate or a human.

## Status

v0.0.1. All eight phases complete. The deterministic core, disputes, hardening, integrations, the conversational shell, and the model critic are done. 24 of 24 acceptance scenarios pass. 1197 tests pass. mypy, ruff, and format checks are clean.

The model critic (C-011) runs on Inception Labs' mercury-3 via an OpenAI-compatible endpoint. It is opt-in: set `NYX_LLM_CRITIC_ENABLED=true` and provide `NYX_LLM_API_KEY`.

## Quick start

```bash
uv sync
uv run alembic upgrade head
uv run nyx doctor
uv run pytest
```

Copy `.env.example` to `.env` and fill in your values. The service binds to 127.0.0.1:8000 by default.

## Configuration

Key settings (all have sensible defaults for local use):

| Setting | Default | Purpose |
|---|---|---|
| `NYX_HOST` | `127.0.0.1` | Bind address |
| `NYX_PORT` | `8000` | Listen port |
| `NYX_AUTH_MODE` | `none` | `none`, `local`, or `api_key` |
| `NYX_AUTH_API_KEY` | (unset) | Required when auth mode is `api_key` |
| `NYX_LLM_ENABLED` | `false` | Enable the LLM subsystem |
| `NYX_LLM_CRITIC_ENABLED` | `false` | Register C-011 |
| `NYX_LLM_PROVIDER` | (unset) | Label for metadata |
| `NYX_LLM_MODEL` | (unset) | Model id, e.g. `mercury-3` |
| `NYX_LLM_PROTOCOL` | `openai` | `openai` or `anthropic` |
| `NYX_LLM_BASE_URL` | (unset) | OpenAI-compatible base URL |
| `NYX_LLM_API_KEY` | (unset) | API key for the LLM backend |
| `NYX_WATCH_ENABLED` | `false` | Enable the adapter watcher |
| `NYX_REDACTION_ENABLED` | `true` | Redact sensitive data in logs |

## Running the service

```bash
uv run uvicorn nyx.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

Health check: `GET /api/v1/health`. Readiness: `GET /api/v1/ready`.

## Auth

In `api_key` mode, pass `X-API-Key: <key>` on every request. Health and readiness endpoints are exempt.

```bash
curl -H "X-API-Key: your-key" http://127.0.0.1:8000/api/v1/findings
```

## Documents

- `docs/SPEC.md` — the research and implementation contract (authoritative).
- `docs/architecture.md` — module map and boundaries.
- `docs/threat-model.md` — trust boundaries, threats, and defenses.
- `docs/evidence-model.md` — what each evidence kind does and does not establish.
- `docs/api.md` — HTTP API contract.
- `docs/CLI-CONTRACT.md` — the CLI's machine interface.
- `docs/audit-verification.md` — the hash chain, its trust limits, and external checkpoints.
- `docs/operations.md` — deployment and operational guidance.
- `docs/adr/` — consequential design decisions.
- `AGENTS.md` — binding rules for coding agents working here.

## Demonstrations

```bash
uv run nyx seed demo          # Ghost Factory fixtures, through the ordinary ingestion gateway
uv run nyx audit checkpoint   # anchor the chain out-of-band (ADR 0012)
uv run nyx doctor --probe     # real reachability, opt-in
uv run nyx watch --once       # run one watcher cycle
```

The seed is idempotent: a second run produces duplicates, not new rows.

## CI

The CI pipeline runs on every push to master and on pull requests:

1. Tests (`uv run pytest`)
2. Types (`uv run mypy`)
3. Lint (`uv run ruff check .`)
4. Format (`uv run ruff format --check .`)
5. Migrations (fresh database, `alembic upgrade head`)

## License

Apache License 2.0 — see `LICENSE`. New runtime dependencies must remain compatible with Apache-2.0.
