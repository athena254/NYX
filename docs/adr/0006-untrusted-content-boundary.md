# 0006. Untrusted content boundary

**Status:** Accepted (Phase 0, 19 October 2026).

## Context

NYX ingests repository files, web pages, agent messages, logs, event payloads, evidence descriptions,
and model output. All of these can contain text that looks like an instruction: "ignore previous
instructions", "approve this finding", "run this command", "you now have permission to delete". If NYX
treats retrieved text as trusted instructions, a monitored or compromised system can control the
critic that is supposed to judge it.

## Decision

- **All ingested content is untrusted data.** It is quoted, summarised, or referenced; it is never
  executed and never treated as an instruction to NYX.
- Instructions inside evidence **never change policy, grant tool access, alter permissions, or change
  a disposition.**
- Tool permissions, adapter credentials, and verification allowlists come only from **trusted
  configuration**, never from event text or model output.
- Model prompts explicitly state that evidence is untrusted and that instructions inside it must not
  be followed. This is defence in depth, not the primary control.
- The primary controls are structural: no arbitrary shell execution from events, an allowlisted
  verification adapter, SSRF protection for URL fetches, path-traversal protection for file evidence,
  and least-privilege credentials scoped per adapter.
- LLM critic output is validated against a strict schema; unknown evidence IDs are rejected, and model
  output can never mark a disposition or confirm a finding by itself.

## Consequences

- NYX can safely ingest hostile content and still report on it.
- Prompt-injection fixtures are a required security test in Phase 4 and are re-run against model
  critics in Phase 6.
- The system must keep a clear separation between "data" and "policy" everywhere, including logs,
  error messages, and adapter input.
- Some legitimate automation is deliberately not built: NYX will not act on instructions found in the
  systems it observes, even when those instructions look reasonable.

## Alternatives considered

- **Sanitise instructions out of incoming text.** Insufficient and brittle: paraphrasing, encodings,
  and indirect instructions defeat keyword filtering. It can be an additional layer but cannot be the
  boundary.
- **Trust content from allowlisted systems.** Rejected: an allowlisted system can still be compromised
  or mistaken; trust in identity is not trust in content.
- **Let a model decide whether text is an instruction.** Rejected as a primary control: it makes the
  attacker's target the same component being defended. Human review plus structural isolation is
  safer.
- **No model critics at all.** Rejected as an absolute, but the MVP ships without them and never lets
  them hold authority. See ADR 0004.
