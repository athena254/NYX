# 0001. Python 3.14 + FastAPI + Pydantic v2

**Status:** Accepted (Phase 0, 19 October 2026).

## Context

NYX must ingest events and claims, validate them, persist them, run critics, and expose an API and CLI.
The scaffold already declared a Python project (`pyproject.toml`, `requires-python = ">=3.12"`), a
`src/nyx` layout, FastAPI, Pydantic v2, and Typer. `docs/SPEC.md` requires strict schema validation,
structured errors, a local-first default, and strong property-based testing.

The sibling `Drift` project is understood to use a comparable Python stack. Its interfaces have not
been inspected in this repository, and no claim is made here about its internals. The instruction to
match it is a consistency goal, not a verified integration.

## Decision

Implement NYX in Python, run it on the scaffold's Python 3.14.7 environment, and use:

- **FastAPI** for the versioned HTTP API and generated OpenAPI.
- **Pydantic v2** for every request, response, config, and domain schema, with JSON Schema exported to
  `schemas/`.
- **Typer** for the `nyx` CLI.
- **Hypothesis** for property-based tests over state transitions, canonicalisation, redaction,
  deduplication, and malformed payloads.
- **myPy strict** and **Ruff** as the type and lint gates.

The service stays single-process and local-first in the MVP.

## Consequences

- One language and one runtime for API, CLI, critics, and adapters; no cross-language serialization
  boundary inside the MVP.
- Pydantic models are the single source of truth; the hand-written JSON Schemas in `schemas/` must be
  kept in agreement by a Phase 1 contract test.
- Hypothesis can generate edge cases from the same typed models, which is the strongest available
  story for testing lifecycle and parsing invariants.
- FastAPI's dependency injection and OpenAPI make the API/CLI consistency contract testable.
- Python model calls to optional LLM providers remain behind an interface; no provider is mandatory.
- Strict mypy adds upfront friction, especially around SQLAlchemy 2.x typing, but catches interface
  drift early.

## Alternatives considered

- **Node/TypeScript service.** Strong async ecosystem, but splits the stack from `Drift` and weakens
  the Hypothesis-style property testing story without adding required capability.
- **Go or Rust service.** Good single-binary deployment and type safety, but slower iteration for the
  domain-heavy lifecycle and weaker schema-to-model reuse for Pydantic-equivalent validation.
- **Python without FastAPI.** A minimal WSGI/ASGI framework would reduce dependencies but lose
  OpenAPI generation and the ecosystem that makes the API contract test cheap.
- **Dataclasses instead of Pydantic.** Lower dependency weight, but loses built-in JSON Schema
  generation, coercion rules, and settings validation that the SPEC requires.
