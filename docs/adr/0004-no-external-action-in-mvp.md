# 0004. No external action in the MVP

**Status:** Accepted (Phase 0, 19 October 2026).

## Context

NYX is an oversight and criticism service that observes other systems. It could be given authority to
block, roll back, revoke, deploy, delete, or spend. The SPEC requires that high-impact output is a
recommendation for an authorised human or policy gate, that NYX must not independently modify another
system, and that enforcement belongs to a separate authorised mechanism. Infrastructure with broad
tool and network access is exactly where an oversight service must not become an unpoliced actor.

## Decision

- The MVP performs **no external action**: no edits, deploys, deletions, rollbacks, permission
  changes, credential revocation, or spending in any monitored system.
- Findings, dispositions, and escalations produce a **recommendation** with a clear label and the
  authority required to act on it.
- Any enforcement is implemented outside NYX by a separately authorised mechanism that consumes a NYX
  recommendation. NYX defines the recommendation contract but does not implement the actuator.
- Adapters in the MVP are read-only observers. They do not expose mutation capabilities, and no
  adapter may silently gain one.
- Automatic external action based solely on a model-generated critique is prohibited.
- A verification tool runs only through an allowlist with resource limits; it never executes commands
  derived from event text.

## Consequences

- NYX cannot be a single point of power or accidentally cause harm through an automated response.
- Incidents need a human or policy gate to close the loop; NYX cannot remediate on its own. This is a
  deliberate safety cost, not an oversight.
- The recommendation contract must be precise enough that an external enforcement mechanism can act
  without reinterpreting NYX's reasoning.
- Tests must prove the absence of a mutation path, not merely its disuse.

## Alternatives considered

- **Full remediation authority behind an approval flag.** Rejected for the MVP. A flag is not an
  authority boundary, and the blast radius of a critic bug or prompt injection is unacceptable.
- **Automatic blocking of monitored systems.** Rejected: monitoring is not prevention, and a false
  positive would cause an outage.
- **Recommendations plus built-in rollback for "obvious" cases.** Rejected: "obvious" is exactly the
  judgement NYX is supposed to make inspectable and contestable, not automatic.
- **No recommendation output at all.** Rejected: the SPEC requires a recommended next step; the
  distinction is that it is labelled a recommendation and never executed by NYX.
