# Phase 6 finish plan — the live calibration, the golden set, and the push

**Source:** `docs/SPEC.md` §19 (Phase 6), §17.2 (scenarios 15 and 19), §13, §16; ADR 0005, ADR 0009,
ADR 0013; `docs/implementation/roadmap.md` §Phase 6; `docs/golden-set.md`; `docs/implementation/phase-6-plan.md`;
`docs/implementation/phase-6-evaluation.md`.
**Owner of this plan:** the orchestrating agent. **Status:** research complete, work starting
26 October 2026.
**Phase 6 exit criterion (SPEC §19):** *schema validation, evidence-ID validation, prompt-injection
tests, provider-failure tests, and documented evaluation results pass.*

**Verified against the code and the machine, 26 October 2026**, branch `master`, HEAD `907dbae`,
working tree clean:

```
uv run pytest      → exit 0 (1141 passed, 1 skipped, matching commit 907dbae's message)
uv run nyx golden generate --spec <4-clause spec> --json
                   → clauses 4, drafted 0, skipped 1, failed 3
```

Every claim about existing code below was read, not inferred from a filename. Every claim about
behaviour below was produced by driving the real path, not by reading the unit tests.

---

## 1. What is actually unfinished

The tree is clean and every phase is committed, so "unfinished" is not "uncommitted". It is three
things, and the first two are invisible from `git log`.

| # | Item | Evidence | State |
| --- | --- | --- | --- |
| 1 | **The live calibration run** | `evals/llm-critic/thresholds.json` is `{"status": "pending", "metrics": null}` | not performed |
| 2 | **The golden set is empty** | `docs/golden-set.md` §"What this does not establish": "this layer has produced none against a live provider" | no cases exist |
| 3 | **Nine commits have never been through CI** | `gh run list`: last pipeline ran 24 Oct on `676b8d9`; `git status -sb`: `ahead 9` | unpushed |

Item 1 is the only item the roadmap names as remaining. Items 2 and 3 are the same class of gap the
repository has been bitten by twice before: a slice's own tests assert the *shape* of what it
returns, and nothing asserts that the rest of the system can *consume* it.

---

## 2. Three defects, found by driving the real path

None of these were reported by the tests, because each test's fake transport returns exactly the
shape the code expects. Each was found by calling the real provider.

### D1 — the drafting prompt never sends the schema

`CaseGenerator.build_prompt()` (`src/nyx/evals/generate.py`) ends with the line
`"Return JSON matching the schema."` — and **the schema is not in the prompt**. `self._schema` is
held by the generator and passed to `transport.complete(schema=...)`, but
`SyncCriticTransport.complete()` explicitly does not send it:

> `schema` is not sent to the provider: the critic already embeds the JSON Schema in the prompt
> and validates the response itself. (`src/nyx/critics/llm_transport.py:70-72`)

That is true for `LlmCritic`, which does embed it (`src/nyx/critics/llm.py:443`). It is false for the
generator. So the model is asked to match a contract it has never seen.

**Observed:** the real provider's response to a drafting prompt was

```json
{"name": "Disposition Recorded Without Reviewer", "description": "...", "claim": "...",
 "agent": "AutomatedAuditSystem", "event": {"type": "DispositionCreated", ...},
 "evidence": [{"dataSource": "audit-log", ...}]}
```

against a schema whose required keys are `case_id`, `title`, `claim.statement`, `claim.claim_type`,
`event.event_type`, and `evidence[].kind/title/description`. The model invented a plausible shape and
the run reported it as a failure. `drafted: 0` out of 4 clauses.

### D2 — a markdown-fenced answer is rejected as "not valid JSON"

Both consumers call `json.loads(raw)` on the transport's return value. The provider wraps its answer
in ` ```json ... ``` `, so `json.loads` raises `Expecting value` and the clause is skipped.

**Observed twice, independently:**

- the drafting probe above returned 1041 characters beginning `'```json\n{\n  "name": ...'`;
- the live eval recorded `"model": "error"` with rationale *"The LLM response was not valid JSON."*
  on one of the eight corpus cases.

Fail-closed is the right *default*, but a code fence is a wrapper, not a refusal. The model answered
correctly and the plumbing threw the answer away — which is worse than a refusal, because a
refusal is at least legible.

### D3 — `nyx golden generate` reports a failure count with no reason

The CLI's payload carries `"failed": 3` and nothing else (`src/nyx/cli/golden.py`). `DraftOutcome`
already carries the `error` string; the command discards it. An operator sees "3 failed" and has no
way to learn why without re-running each clause by hand. This is the same shape as the `nyx doctor`
exit-code trap the repository already records: **a structured report must report its own failures.**

---

## 3. Design decisions

### 3.1 One JSON extraction, shared by both consumers

Add `extract_json_document(text: str) -> str` to `src/nyx/llm/provider.py`, next to `parse_response`,
and call it from both `LlmCritic.run` and `CaseGenerator.draft` before `json.loads`.

- It removes a leading/trailing markdown fence and any language tag, and returns the first
  balanced `{...}` or `[...]` span in the text. It does **not** repair malformed JSON.
- **The schema is still enforced afterwards, unchanged.** Extraction widens what is *parsed*; it
  does not widen what is *accepted*. A fenced document that still violates the schema is rejected
  exactly as before, and every existing negative test must keep passing.
- It must **raise**, not return the input unchanged, when it cannot find a JSON span — a silent
  pass-through would turn a garbage response into a `json.JSONDecodeError` one layer later with a
  worse message.

### 3.2 The schema goes into the drafting prompt

`build_prompt()` gains a `schema: dict[str, Any]` parameter and embeds it as
`json.dumps(schema, indent=2, sort_keys=True)`, in the same shape `LlmCritic._build_prompt` already
uses. The prompt's final line becomes a reference to the block above it rather than to a contract
the model cannot see.

`PROMPT_TEMPLATE_VERSION` in `src/nyx/evals/generate.py` is bumped `1.0.0` → `1.1.0`, because a
stored case must say which template produced it.

### 3.3 Failures are reported per clause

`nyx golden generate` prints, for every clause it did not draft, the clause text and the reason, in
both `--json` and human output. A count without a reason is a command that reports a failure it
cannot explain.

### 3.4 Thresholds are floors, and the test already allows it

`tests/unit/test_eval_thresholds.py` accepts `status` in `{pending, current}` and, when `current`,
requires `metrics.min_accuracy` in `[0, 1]` with `max_false_positives` / `max_false_negatives` ≥ 0.
The thresholds file is replaced with the measured numbers **and their margin**, and the margin is
stated in the file so a reader cannot mistake a floor for a calibration claim.

---

## 4. Slices

`src/nyx/api/app.py` and `src/nyx/cli/main.py` are **orchestrator-only**; no slice below touches them.
This plan is sequential — each slice's output is the next slice's input.

| Slice | Deliverable | Files | Tests | Gate |
| --- | --- | --- | --- | --- |
| **F1** | `extract_json_document`, used by both consumers | `src/nyx/llm/provider.py`, `src/nyx/critics/llm.py`, `src/nyx/evals/generate.py` | `tests/unit/test_llm_json_extraction.py`, extend `tests/unit/test_llm_critic_transport.py`, extend `tests/unit/test_evals_golden_set.py` | all four + a probe over the real socket |
| **F2** | Schema embedded in the drafting prompt; template version bumped | `src/nyx/evals/generate.py` | `tests/unit/test_evals_golden_set.py` (assert the schema is present, and that a schema-shaped answer now validates) | all four |
| **F3** | Per-clause failure reporting in `nyx golden generate` | `src/nyx/cli/golden.py` | `tests/integration/test_golden_cli.py` | all four + CLI contract |
| **F4** | **Live calibration run**, recorded; thresholds replaced | `docs/implementation/phase-6-evaluation.md`, `evals/llm-critic/thresholds.json`, `docs/implementation/evidence/live-run-*.json` | `tests/unit/test_eval_thresholds.py` | runner output + all four |
| **F5** | **Golden set from a real spec**: generate, review, export, score | `evals/golden/cases.jsonl`, `evals/golden/golden.jsonl`, `docs/implementation/golden-set-run.md` | `tests/unit/test_eval_corpus.py` | runner output + all four |
| **F6** | Status sync, then push so CI runs on it | `docs/implementation/roadmap.md`, `docs/implementation/acceptance-tests.md`, `docs/implementation/task-checklist.md`, `docs/implementation/HANDOFF.md`, `docs/golden-set.md` | — | `gh run list` green |

**Outcome of F4–F6 (26 October 2026):** both delivered. F5's set is two cases, both approved by
the owner — the provider refuses fifteen of the seventeen labelled clauses, which is a finding
about dmr-X and not a defect in NYX. See `docs/implementation/golden-set-run.md` §3.

Dependency order: F1 → F2 → F3 → F4 → F5 → F6.

---

## 5. Risks

| # | Risk | Mitigation |
| --- | --- | --- |
| R1 | Extraction weakens the fail-closed guarantee | The schema is still enforced after extraction; the new test asserts a fenced **invalid** document is still rejected, and every pre-existing negative test must stay green |
| R2 | The provider is slow or rate-limits (observed ~20–40 s per call) | Runs are opt-in and recorded; a provider failure is recorded as *failure to obtain an assessment*, not as a critic-quality result (Phase 6 plan R4) |
| R3 | The provider ignores the schema even when it is sent | That is a recorded result, not a hidden one: F5 reports per-case drafts and failures, so "the model does not follow the contract" is visible rather than averaged away |
| R4 | The golden set grows slowly | Each case needs a human approval; the review step is the independence, and automating it would make the set the spec grading itself (ADR 0013) |
| R5 | Pushing surfaces a red CI | That is the point of F6. The nine commits have never been gated; a red pipeline is a finding about the tree, reported as such |

---

## 6. Definition of done

- `scripts/eval_llm_critic.py --live` has been run against the configured provider, its report is
  recorded in `docs/implementation/phase-6-evaluation.md`, and `evals/llm-critic/thresholds.json`
  carries measured floors with a stated margin.
- `nyx golden generate --spec docs/SPEC.md` drafts cases from the real provider, the drafts are
  reviewed, `nyx golden export` writes them, and the runner scores the approved set.
- Every defect found on the way is fixed, with the test that would have caught it.
- The full gate set passes with real output reported, and the branch is pushed so CI runs on it.
- The docs state what is still not established: the golden set's size, the labelling rule's bias,
  and that no model-quality claim is stronger than one recorded run against one provider.
