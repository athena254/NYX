# Independent safety-invariant audit — Phases 0, 1a, 1b

**Date:** 19 October 2026
**Method:** a separate, adversarial, read-only audit performed by a different agent than the one that
wrote the code. It read all source files under `src/nyx/`, all test files, the ADRs, `docs/SPEC.md`,
`docs/threat-model.md` and `AGENTS.md`; traced the ingestion pipeline end to end; and specifically
tried to defeat the claim-scope validator, the redaction pipeline ordering, the idempotency dedupe,
the audit hash chain, and the secret-in-metadata validator (including nested keys and case variation).
**Scope:** Phases 0, 1a and 1b only. Findings against unimplemented later phases are recorded as
`UNENFORCED` and are expected — they are the work of Phases 2-6, not defects in Phase 1.

Orchestrator note: this audit is itself a claim, and was treated as one. Its two most concrete
findings were independently reproduced before being recorded (see "Verified findings" below).

## Results by invariant (SPEC §3.2)

| # | Invariant | Status | Evidence |
|---|---|---|---|
| 1 | No `confirmed` finding without rationale + evidence | UNENFORCED | Findings are Phase 2. `FindingStatus.CONFIRMED` exists (`domain/enums.py:170`); no code creates or validates findings. |
| 2 | Model claim never labelled deterministic verification | UNENFORCED | LLM critics are Phase 6. `CriticType.LLM` (`domain/enums.py:132`) exists with no enforcement. |
| 3 | Hash described as integrity identifier, not proof | COMPLIANT | `domain/canonical.py:3-7`, `domain/evidence.py:3-5`, `domain/audit.py:3-5`, ADR 0005. No violating code. No automated guard against future misuse. |
| 4 | Missing telemetry = unknown, not misconduct | UNENFORCED | Critics are Phase 2. `CriticResultStatus.INCONCLUSIVE` (`domain/enums.py:141`) exists, unimplemented. |
| 5 | Rebuttals append; never erase | UNENFORCED | Disputes are Phase 3. The audit log is append-only (`audit/writer.py:60-97`) but covers ingestion only. |
| 6 | No external action in the MVP | COMPLIANT | Compliant by construction: no code path can mutate another system. No adapters implemented. ADR 0004. |
| 7 | Every critic result records type/version/inputs/time/outcome | UNENFORCED | Critics are Phase 2. Enums exist (`domain/enums.py:129-141`); no `CriticRun` model. |
| 8 | Sensitive payloads not logged; redact before storage/model | COMPLIANT (with gaps) | Redaction runs before persistence: `ingestion/gateway.py:269-273` → `:331`. Test `test_redaction_persistence.py:34-54`. Secret metadata keys rejected: `domain/systems.py:57-64`, `domain/base.py:132-164`. Gaps below. |
| 9 | All external content treated as untrusted | UNENFORCED | ADR 0006 documents it; no quoting, delimiting, SSRF or path-traversal code exists yet (Phase 4/5). |
| 10 | Findings disputable; disposition changeable with audit trail | UNENFORCED | Disputes are Phase 3. Enums exist (`domain/enums.py:167-169`); no implementation. |

## Verified findings

### V-1 — Logging formatter does not redact record attributes (latent) — FIXED

**Claim:** `observability/logging.py:60-62` copies every non-reserved `LogRecord` attribute into the
JSON payload without redaction.

**Reproduced by the orchestrator:**

```
log.info('ingesting event', extra={'api_key': '«redacted:sk-…»', 'note': 'hunter2'})
-> {"timestamp": "...", "level": "INFO", "logger": "probe", "message": "ingesting event",
    "request_id": "-", "api_key": "sk-liv...T123", "note": "hunter2"}
LEAKED api_key : True
LEAKED note    : True
```

**Severity: latent, not active.** No current call site passes sensitive data via `extra=`. Every
existing `extra=` use was checked: `api/errors.py:80,139,154` pass `request_id`/`code`/`error_type`;
`api/middleware.py:37,50` pass `request_id`/`method`/`path`/`status_code`/`duration_ms`. Request
bodies and headers are never logged. So nothing leaks today — but the formatter is a trap for the
first `extra={"token": ...}` call, and later phases (adapters, verification jobs) will add call sites.

**Status: FIXED (2026-10-19).** `JsonFormatter.format()` now passes the collected extra attributes
through `nyx.ingestion.redaction.redact()` — the same rules the ingestion pipeline uses, no second
implementation — before merging them into the JSON payload, and fails closed (drops extras) if
redaction raises. Non-secret extras (`request_id`, `method`, `path`, `status_code`, `duration_ms`)
pass through unchanged; secret-shaped values and secret-named keys, including nested dicts and lists,
are replaced with `[REDACTED:…]` placeholders.

Proven by `tests/unit/test_logging_redaction.py` (5 tests: top-level secret, nested dict, nested list,
benign extras preserved, message integrity) and independently reproduced by the orchestrator: a
top-level `api_key` and a nested `token` both render as `[REDACTED:secret_key_value]`, while
`request_id` and `status_code` survive unchanged.

**Owner:** Phase 4 (delivered early).

### V-2 — Redaction coverage gaps

**Claim:** short low-entropy secrets under non-secret key names are not caught, and non-string values
are not covered.

- `{"note": "hunter2"}` is not redacted — the value carries no secret shape and the key is not
  secret-ish. This is a genuine limit of pattern-based redaction, not a bug: redacting every short
  string would destroy legitimate payloads. It must be **documented as a limit**, not claimed as
  coverage.
- Non-string secrets (numbers, booleans) are not examined.

**Owner:** Phase 4 — document the limit honestly and extend coverage where it is safe to do so.

## Top gaps in priority order

1. **`auth_mode` is not enforced.** `config.py:58` defines it; no middleware or dependency reads it.
   Setting `NYX_AUTH_MODE=api_key` without a checking middleware creates a false sense of security.
   Owner: Phase 4.
2. **No rate limiting.** SPEC §9.2 requires it. Unbounded ingestion can exhaust disk and CPU.
   Owner: Phase 4.
3. **No SSRF protection.** No URL-fetching code exists yet, so nothing is currently exploitable —
   but the first adapter that fetches a URL inherits the gap. Owner: Phase 5, with the Phase 4 rules.
4. **No path-traversal protection.** Same shape: no file-reading code yet. Owner: Phase 5.
5. **V-1 logging redaction.** Owner: Phase 4.
6. **V-2 redaction coverage.** Owner: Phase 4.
7. **No prompt-injection defence.** Structural isolation is the real control and it does not exist
   yet. Owner: Phase 4, exercised against the model path in Phase 6.
8. **Audit hash-chain scope.** The chain (`audit/writer.py:60-97`) detects accidental or partial
   mutation within the stored sequence. It is not a signature or transparency log: a privileged
   attacker who can rewrite the database can recompute the chain. This is already documented in
   `docs/threat-model.md` and ADR 0003 and is an accepted limitation, not a defect.

## Conclusion

Phase 1 is sound. The three `COMPLIANT` invariants are genuinely enforced, and invariant 8 was
independently reproduced end to end: a secret pushed through the live HTTP API never reached the
database. The `UNENFORCED` rows are the planned work of Phases 2-6. The actionable output of this
audit is gaps 1, 2, 5 and 6, all of which are now required items in the Phase 4 task prompt.
