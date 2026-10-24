# Caveat-hardening plan — license, local-git objects, audit checkpoints, doctor probe, onboarding

**Source:** `README.md` §Status/§License; `docs/implementation/roadmap.md` §Phase 5 "Honest
limitations"; `docs/audit-verification.md` §"What this does not establish"; ADR 0004 (no external
action), ADR 0008 (adapter boundary), ADR 0009 (unattended operation);
`docs/implementation/HANDOFF.md` (23 October 2026 updates); `docs/SPEC.md` §5.8, §14, §16, §19.
**Owner of this plan:** the orchestrating agent. **Status:** accepted by the project owner,
24 October 2026.

**Decisions taken by the owner:**

1. License: **Apache-2.0**.
2. Local-git limitation: lift to **loose and packed object reading** (standard library only).
3. All three small slices are in scope: audit checkpoints, `nyx doctor --probe`, seed demo.
4. Live evaluation against dmr-X happens only with the owner's go-ahead at execution time.

**Last verified against the code:** 24 October 2026, HEAD `644a44c`, working tree clean.
`uv run pytest` green (648 passed, 1 skipped baseline), `mypy`/`ruff` clean.

Every claim below was read in the source, not inferred from a filename.

---

## 1. Current state of each caveat

### 1.1 License

No `license` field and no classifier in `pyproject.toml`; no `LICENSE` file; `README.md:45-47`
says "Not yet chosen — decide before any external distribution." Every installed dependency is
permissive (MIT / BSD-2 / BSD-3 / Apache-2.0 / MPL-2.0), so Apache-2.0 introduces no conflict.

### 1.2 Local-git provider

`src/nyx/adapters/git_ci/local_git.py:16-20` documents the limitation: it reads `.git/HEAD`,
`.git/refs/**`, `.git/packed-refs`, and `.git/logs/**` with `pathlib`, **not git objects** — so no
commit contents, file lists, or diffs. The module deliberately starts no process; a `src/` grep
gate forbids `subprocess`, `os.system`, `shell=True`, `eval(`, `exec(` anywhere under `src/`.
ADR 0008 records the same. A library (dulwich, GitPython, pygit2) was rejected during Phase 5's
dependency review; this plan honours that verdict and uses `zlib` from the standard library.

### 1.3 Audit chain

`docs/audit-verification.md:33-42`: the chain detects mutation **within the stored sequence**. An
attacker who rewrites the whole database and recomputes every hash defeats it, as does an
administrator with write access. That is recorded honestly, but it is the one limitation that an
operator can partly close **without** a signing service: pin the chain head outside NYX.

### 1.4 `nyx doctor`

`src/nyx/cli/main.py:497-536` reports what is configured/registered and explicitly does not probe
reachability. The Phase 4 lesson (defect #7) was "never report configuration as availability"; the
inverse (never claiming any availability at all) leaves the operator without a way to ask "is it
reachable right now?".

### 1.5 Smaller recorded items

| Item | Where recorded | This plan |
| --- | --- | --- |
| Per-process rate limiting | roadmap Phase 4 limitations | Documented in operations/threat model; shared store is out of scope |
| Container image built only in CI (no Docker on this host) | HANDOFF 23 October | Documented; no code change possible here |
| Eight unfixed base-image CVEs, `.trivyignore` re-check dates | `.trivyignore` | Documented in operations; re-check note kept |
| Ghost Factory seed never landed | roadmap Phase 1 open box | `nyx seed demo` slice |
| ADR 0010 referenced in code/commit but file missing | `src/nyx/config.py:147`, commit `cdab828` | Write the missing ADR 0010 |
| Roadmap/README status stale vs. watch + agent-transcript work | roadmap summary | Docs sync in the final slice |

---

## 2. Slices

`src/nyx/api/app.py` and `src/nyx/cli/main.py` remain orchestrator-only. Delegated slices get
exclusive file lists.

| Slice | Deliverable | Files (owner) | Tests | Gates |
| --- | --- | --- | --- | --- |
| **H1** License | Apache-2.0 `LICENSE`, `pyproject.toml` license/classifier, README §License, ops note on dependency license policy | `LICENSE`, `pyproject.toml`, `README.md`, `docs/operations.md` | none (no runtime change) | all four |
| **H2a** Loose git objects | `ObjectReader`: zlib loose-object parsing; commit metadata (author, committer time, parents, tree, message) and tree file lists; `LocalGitClient` enriches runs from real objects, keeps `conclusion=None` | `src/nyx/adapters/git_ci/objects.py`, `local_git.py` | `tests/unit/test_git_ci_objects_loose.py`, extended local-git unit tests | all four |
| **H2b** Packed git objects | `.idx` v2 + `.pack` reading with OFS/REF delta resolution; corrupt pack → `GitCiError(unavailable)`; contract cases after `git gc` | `objects.py` (owner continues), contract test cases, ADR 0011 | `tests/unit/test_git_ci_objects_packed.py`, extended `tests/contract/test_git_ci_adapter.py` | all four + contract suite |
| **H3** Audit checkpoints | `nyx audit checkpoint` appends `{sequence, head_hash, recorded_at}` to an external JSONL file; `nyx audit verify --against FILE` detects a fully-recomputed rewrite; ADR 0012; docs narrowed honestly | `src/nyx/audit/checkpoint.py`, `src/nyx/audit/verifier.py`, `src/nyx/cli/main.py` (orchestrator), `docs/audit-verification.md`, ADR 0012 | `tests/unit/test_audit_checkpoint.py`, extended `tests/integration/test_audit_integrity.py` | all four |
| **H4** doctor `--probe` | Opt-in bounded reachability for the LLM endpoint and constructible adapters; 5s cap; secret-safe; probe failures are failed checks | `src/nyx/cli/main.py` (orchestrator), `src/nyx/adapters/factory.py` (only if a helper is needed) | extended `tests/integration/test_health_and_doctor.py` | all four |
| **H5** Seed demo + docs sync | Promote fixtures to `examples/ghost-factory/`, `nyx seed demo` through the ingestion gateway (idempotent), missing ADR 0010, README/roadmap sync | `src/nyx/seed/`, `examples/ghost-factory/*`, `src/nyx/cli/main.py` (orchestrator), `docs/adr/0010-agent-transcript-adapters.md`, docs | CLI contract additions, `tests/integration/test_seed_demo.py` | all four |

Dependencies: H2b follows H2a; H1/H3/H4/H5 are independent of Phase 6 and may interleave.

---

## 3. Design notes

### 3.1 Git object reading (H2a/H2b)

- Loose objects: `objects/<2-hex>/<38-hex>` → `zlib.decompress` → `<type> <size>\0<payload>`.
- Packs: `.idx` v2 fanout + SHA table + offset table; pack header `PACK`; object header varint
  type/size; `OFS_DELTA` (negative offset) and `REF_DELTA` (base SHA) applied with the delta
  instruction stream. All standard library.
- Bounds: every read is size-capped and every failure maps to a `GitCiError` code; no traceback
  escapes.
- Honest scope: commit metadata and tree file lists. **Textual diffs are not produced**; a
  file-name change set against the first parent is the maximum claimed, and the docs say so.
- Tests build real repositories with the `git` CLI inside `tests/` (the subprocess gate covers
  `src/` only) so loose, packed, and delta-encoded objects are exercised for real.

### 3.2 Audit checkpoints (H3)

- The checkpoint is append-only JSONL written to a path the operator chooses; the default lives in
  `data_dir` and the docs say plainly that a checkpoint stored next to the database is weaker than
  one copied elsewhere.
- `verify --against` walks the chain to the checkpointed sequence and compares the entry hash. A
  rewritten database that is internally consistent no longer verifies against an external
  checkpoint — this is the *only* new guarantee, and it is stated as such.
- A missing/short checkpoint file is an explicit refusal, not a pass.

### 3.3 `nyx doctor --probe` (H4)

- Reachability only; the command never mutates, never ingests, never runs a critic.
- LLM: bounded `GET` against the configured base URL (protocol-appropriate models endpoint).
- Adapters: `health()` for constructible adapters from the shared factory (ADR 0009), DEGRADED
  reported as a failed check **because the operator explicitly asked for a probe**.
- Without `--probe`, doctor's existing semantics are unchanged.

### 3.4 Seed demo (H5)

- Fixtures move from `tests/fixtures/` to `examples/ghost-factory/` as read-only data; the seed
  command reads that directory and pushes through the ordinary `IngestionGateway`, so redaction,
  validation, deduplication, and audit apply exactly as for producer traffic.
- Re-running the seed is a no-op by identity; the test asserts row counts do not grow.

---

## 4. Risks

| # | Risk | Mitigation |
| --- | --- | --- |
| R1 | Packfile parsing is easy to get subtly wrong | Real `git gc` fixtures, delta cases, and a fuzz-ish malformed-input test asserting a `GitCiError` rather than a traceback |
| R2 | The checkpoint gives a false sense of tamper-proofing | Docs state the exact guarantee and its precondition (checkpoint stored outside the attacker's reach) |
| R3 | `doctor --probe` turns a healthy service into a failed check because a third party is down | Probe is opt-in and the failure names the dependency; exit code semantics documented |
| R4 | Seed fixtures drift from test fixtures | One source directory (`examples/ghost-factory/`); tests read the same files |
| R5 | The missing ADR 0010 stays missing | Written in H5 and cross-checked against the existing adapter code |
| R6 | Fixture data contains secrets or private data | Fixtures already exist and were committed; the seed test asserts no secret-like keys persist |

---

## 5. Definition of done

- Apache-2.0 is recorded in `LICENSE`, `pyproject.toml`, and the README.
- The local-git provider reads loose and packed commit/tree objects with no new dependency and no
  subprocess under `src/`; the remaining textual-diff limitation is documented.
- `nyx audit verify --against` detects a fully recomputed rewrite; the docs say what it still does
  not establish.
- `nyx doctor --probe` reports real reachability without leaking secrets.
- `nyx seed demo` loads the Ghost Factory demonstration idempotently.
- README's caveat list and the roadmap summary match what the code actually does.
- All four gates pass, with real output reported.
