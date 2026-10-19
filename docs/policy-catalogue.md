# Policy catalogue

Versioned, deterministic rules registered in the default policy registry
(`nyx.policies.engine.default_registry`). Each rule is a pure function over a payload
dict; a decision is reproducible and can be tied to an exact rule revision via
`PolicyRegistry.digest`.

## approval.required-recorded

- **Version:** 1.0.0
- **Description:** A record that claims a high-impact completion must carry an
  approval reference.
- **Payload keys:** `requires_approval: bool`, `approval_ref: str | None`
- **Applies when:** `requires_approval` is true.
- **Violation means:** the record requires approval but no `approval_ref` is recorded.

## approval.no-self-approval

- **Version:** 1.0.0
- **Description:** The approver must not be the claimant.
- **Payload keys:** `claimant_principal_id: str | None`, `approver_principal_id: str | None`
- **Applies when:** both a claimant and an approver principal are recorded.
- **Violation means:** the approver is the same principal as the claimant.

## completion.evidence-required

- **Version:** 1.0.0
- **Description:** A completion claim must have at least one linked verification
  evidence item.
- **Payload keys:** `linked_verification_count: int` (plus `claim_type`, which gates
  applicability)
- **Applies when:** `claim_type` is `completion`.
- **Violation means:** fewer than 1 linked verification evidence item is recorded.
