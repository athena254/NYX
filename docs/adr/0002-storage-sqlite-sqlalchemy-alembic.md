# 0002. Storage: SQLite via SQLAlchemy 2.x with Alembic migrations

**Status:** Accepted (Phase 0, 19 October 2026).

## Context

NYX stores systems, events, claims, evidence references, critic runs, findings, rebuttals,
dispositions, policy versions, and audit entries. The SPEC requires schema migrations, transaction
boundaries, append-only behaviour where applicable, retention classes, and local-first operation. It
also warns against introducing multiple databases or message brokers in the MVP.

The scaffold already depends on SQLAlchemy 2.1.5 and Alembic 1.20.0 and targets a file-backed SQLite
database for local development.

## Decision

- Use **SQLite (file-backed)** as the only database in the MVP.
- Access it through **SQLAlchemy 2.x** ORM/Core with a single engine and a unit-of-work transaction
  boundary per request or command.
- Manage schema with **Alembic** migrations checked into `migrations/`, applied by
  `uv run alembic upgrade head` and reported by `nyx doctor`.
- Keep **no second database and no message broker** in the MVP. Critic execution is in-process and
  synchronous or backed by a small persisted queue table, not an external queue.
- Keep persistence semantics identical between local and production modes; only the connection string
  and deployment controls change.

## Consequences

- Zero external infrastructure for a developer or reviewer to run the service.
- SQLite's write concurrency is limited. Ingestion must use bounded batches and short transactions;
  a future move to PostgreSQL must be a migration exercise, not a rewrite, so SQLAlchemy usage stays
  portable and dialect-specific SQL is avoided.
- SQLite does not provide row-level security or strong separation between database users. Access
  control is enforced in the application, and the audit chain is documented as not tamper-proof
  against an administrator with the database file.
- Append-only behaviour is enforced in application code and, where practical, with database
  constraints; it is not treated as cryptographically immutable storage.
- Alembic migrations are mandatory for every schema change, including Phase 1's first schema.

## Alternatives considered

- **PostgreSQL from the start.** Better concurrency, but adds an external service to a local-first MVP
  with no demonstrated load.
- **Both SQLite and PostgreSQL.** Rejected: divergent persistence semantics and doubled testing.
- **Embedded document store or append-only log file.** Simpler for events, but loses relational
  constraints, migrations, and queryable joins across findings, evidence, and dispositions.
- **An event broker plus a database.** Rejected for the MVP: operational complexity without a
  demonstrated need, and the SPEC explicitly discourages it.
- **An ORM-free hand-written SQL layer.** More control, but repeats migration, transaction, and typing
  work that SQLAlchemy and Alembic already provide, and weakens the strict-mypy story.
