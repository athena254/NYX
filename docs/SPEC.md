# NYX --- Evidence-Backed Critic and Ecosystem Oversight

## Open-source research, architecture, repository shortlist, and implementation plan

**Research date:** 19 October 2026\
**Status:** Research and architecture recommendation; not a claim that
NYX is already implemented.\
**Purpose:** Define how NYX can observe the wider AI ecosystem,
challenge claims and decisions with inspectable evidence, verify
disputes, and audit itself.

------------------------------------------------------------------------

## Executive recommendation

Build NYX as an **independent, evidence-backed critic for the whole
ecosystem**. It should observe systems, detect inconsistencies and
risks, challenge other systems with evidence, accept rebuttals,
independently verify disputed claims, and record how each case was
resolved.

NYX is not simply a policy checker or another LLM-as-judge. It needs
five distinct capabilities:

1.  **Observation:** Collect events from agents, tools, repositories,
    CI, deployments, memory systems, routers, and policy engines.
2.  **Evidence and provenance:** Preserve source pointers, event
    identity, timestamps, artifact hashes, lineage, and integrity
    metadata.
3.  **Criticism:** Identify contradictions, unsupported claims,
    reasoning gaps, drift, policy violations, security weaknesses, and
    undesirable outcomes.
4.  **Verification and dispute:** Test claims, seek counterevidence,
    request a rebuttal, rerun checks where possible, and record a
    defensible disposition.
5.  **Governance and self-audit:** Route findings to authorized
    decision-makers and ensure NYX's own findings, evidence choices,
    rules, and changes can also be challenged.

**Defining rule:** No serious accusation without inspectable evidence;
no high-impact conclusion without proportionate verification; no verdict
that is immune to challenge.

### Important distinction: evidence is not the same as proof

-   A deterministic test result can establish that a particular test
    passed or failed for a specific input, environment, and commit.
-   A signed attestation can establish the integrity and claimed origin
    of a record under the relevant trust assumptions.
-   A trace can establish that an instrumented event was recorded; it
    cannot prove that uninstrumented events did not occur.
-   An LLM evaluator's conclusion is an assessment, not independent
    proof.
-   Several sources are only truly independent if they do not merely
    repeat the same original claim.

NYX must retain the underlying evidence and limitations rather than
converting every result into a simplistic confidence score.

------------------------------------------------------------------------

## 1. NYX's role in the ecosystem

NYX should watch all connected systems, including (as integrations
become available):

-   **ATHENA:** task coordination, execution reports, approvals, agent
    roles, resource allocation, and governance decisions.
-   **ARGUS:** research claims, investigation chains, source quality,
    conclusions, and uncertainty.
-   **DANNY:** claims about user intent and preferences, with
    appropriate privacy protections and source authority.
-   **NOESIS:** memory provenance, temporal conflicts, derived
    knowledge, source lineage, and changes to remembered information.
-   **DMRX:** request routing, provider/model selection, usage reports,
    policy decisions, and discrepancies between reported and observed
    costs or outcomes.
-   **Ghost Factory:** repository changes, branch/PR state, build and
    test evidence, review, integration, deployment, and maintenance
    claims.
-   **Other agents and services:** tool calls, delegated work, API
    operations, data access, security events, model evaluations, and
    failures.

This list defines the intended scope, not an assumption that every
system already emits the required telemetry.

### NYX is the critic, not the unchecked ruler

NYX should be able to: - Ask hard questions and identify
contradictions. - Require evidence for important claims. - Publish a
finding and request a rebuttal. - Recommend remediation or escalation. -
Trigger a policy-defined gate through a separate, authorized enforcement
mechanism.

NYX should not automatically gain unrestricted power to edit other
systems, rewrite their policies, revoke arbitrary credentials, or
reverse actions. Any blocking, approval, rollback, or permission change
must be authorized by a separately defined governance policy.

------------------------------------------------------------------------

## 2. Open-source repository research

The repositories below are candidates for **borrowing concepts and
selectively integrating components**, not a recommendation to install
every project. Confirm current maintenance, release activity,
dependencies, licensing, security posture, and API stability before
adopting a dependency. Repository descriptions and project maturity can
change.

### 2.1 Observation and AI observability

#### OpenTelemetry Collector

-   Repository:
    https://github.com/open-telemetry/opentelemetry-collector
-   Purpose: vendor-neutral collection, processing, and export of
    telemetry.
-   Borrow: a pluggable collector pipeline, processors, exporters,
    common telemetry conventions, and separation between instrumentation
    and storage.
-   NYX use: receive traces/events from heterogeneous agents and systems
    without binding NYX to one vendor's backend.
-   Limitation: telemetry is only as complete as the instrumentation and
    trust boundary. A collector does not magically observe every action.

#### OpenInference

-   Repository: https://github.com/Arize-ai/openinference
-   Purpose: AI observability conventions and instrumentation that
    complement OpenTelemetry, including model, retrieval, and tool-use
    context.
-   Borrow: semantic conventions for model calls, tool execution,
    retrieval, and parent/child traces.
-   NYX use: normalize events across agent frameworks and correlate tool
    activity with the agent run that initiated it.
-   License shown by repository: Apache-2.0.
-   Source: https://github.com/Arize-ai/openinference

#### Langfuse

-   Repository: https://github.com/langfuse/langfuse
-   Purpose: LLM/agent tracing, prompt management, datasets, evaluation,
    and feedback.
-   Borrow: trace inspection, dataset-driven experiments, evaluation
    history, and debugging workflow.
-   NYX use: candidate UI/backend for inspecting agent runs and
    evaluation outcomes.
-   Decision: evaluate alongside Phoenix; select one primary
    observability/evaluation interface initially.

#### Arize Phoenix

-   Repository: https://github.com/Arize-ai/phoenix
-   Purpose: open-source AI observability and evaluation.
-   Borrow: trace visualization, dataset experiments, evaluator
    workflows, and debugging.
-   NYX use: alternative to Langfuse for viewing traces and evaluation
    results.
-   Decision: do not deploy both Langfuse and Phoenix by default. Keep
    instrumentation portable so the backend can be replaced.

**Recommended baseline:** OpenTelemetry + OpenInference for
instrumentation; one primary backend (Langfuse *or* Phoenix) for the
first implementation.

Sources: - https://github.com/open-telemetry/opentelemetry-collector -
https://github.com/Arize-ai/openinference -
https://langfuse.com/integrations/other/openinference -
https://github.com/Arize-ai/phoenix

### 2.2 Evaluation, adversarial critique, and claim verification

#### Inspect AI

-   Repository: https://github.com/UKGovernmentBEIS/inspect_ai
-   Evaluation suite: https://github.com/UKGovernmentBEIS/inspect_evals
-   Purpose: structured model/agent evaluations using tasks, solvers,
    and scoring.
-   Borrow: repeatable evaluations, configurable tasks, standardized
    results, and a growing evaluation suite.
-   NYX use: regression/evaluation harness for whether agents behave as
    intended under controlled test scenarios.
-   Limitation: an evaluation score is only meaningful for the task,
    setup, and metric used.

#### Promptfoo

-   Repository: https://github.com/promptfoo/promptfoo
-   Purpose: LLM/application evaluation, red-teaming, and CI
    integration.
-   Borrow: adversarial test generation, regression testing,
    configurable criteria, and automated evaluation in CI.
-   NYX use: periodically challenge connected agents and test whether
    changes introduce known safety or reliability failures.
-   Licensing/organizational note: the public project states it is open
    source and MIT-licensed; the project has also announced its
    acquisition by OpenAI. Keep it replaceable if organizational
    independence is a requirement.
-   Limitation: evaluator shortcuts and false positives/negatives are
    possible; validate the precise evaluation behavior and version used.

#### garak

-   Repository: https://github.com/NVIDIA/garak
-   Purpose: vulnerability scanning and adversarial probing of LLMs.
-   Borrow: probe-based testing, vulnerability categories, and
    repeatable model/application scans.
-   NYX use: security and robustness probes for model endpoints and
    selected agent interfaces.
-   Limitation: a model-level scan does not replace application-specific
    authorization and tool-boundary testing.

#### DeepEval

-   Repository: https://github.com/confident-ai/deepeval
-   Purpose: LLM and agent evaluation, including outputs and
    trajectories.
-   Borrow: structured metrics and regression testing for multi-step
    agent behavior.
-   NYX use: evaluate both final results and relevant intermediate
    steps/tool calls.

#### Ragas

-   Repository: https://github.com/vibrantlabsai/ragas
-   Faithfulness metric documentation: https://docs.ragas.io/
-   Purpose: evaluation of retrieval-augmented generation and related AI
    workflows.
-   Borrow: claim-by-claim grounding and checking whether a generated
    answer is supported by retrieved context.
-   NYX use: detect claims unsupported by research documents, code
    evidence, or NOESIS source material.
-   Limitation: LLM-based faithfulness evaluation is not a mathematical
    proof. Preserve the exact cited passages and use deterministic
    checks where available.

**Recommended approach:** do not call every evaluator on every event.
Use a routing layer to select a specialist based on the suspected
failure: - unsupported factual claim → source/claim verification; -
agent trajectory or task-quality issue → Inspect AI/DeepEval-style
evaluation; - adversarial/security concern → Promptfoo/garak; -
retrieval answer → Ragas-style groundedness check; - rule violation →
deterministic policy engine; - software defect → tests, static analysis,
dependency/security scanning.

Sources: - https://github.com/UKGovernmentBEIS/inspect_ai -
https://github.com/UKGovernmentBEIS/inspect_evals -
https://github.com/promptfoo/promptfoo -
https://github.com/NVIDIA/garak -
https://github.com/confident-ai/deepeval -
https://github.com/vibrantlabsai/ragas

### 2.3 Policy-as-code and authorization

#### Open Policy Agent (OPA)

-   Repository: https://github.com/open-policy-agent/opa
-   Documentation: https://www.openpolicyagent.org/docs
-   Purpose: evaluate declarative policies (Rego) against structured
    input.
-   Borrow: testable, versioned, machine-readable policies and
    separation of policy from application code.
-   NYX use: encode verifiable rules such as required approvals,
    mandatory test checks, restrictions on self-modification, and
    evidence requirements for state transitions.
-   Strong candidate for the initial general policy engine.

#### Cedar

-   Repository: https://github.com/cedar-policy/cedar
-   Specification/testing resources:
    https://github.com/cedar-policy/cedar-spec
-   Purpose: policy language and authorization engine focused on
    fine-grained access decisions.
-   Borrow: explicit principal/action/resource authorization and
    rigorous policy validation/testing.
-   NYX use: consider if the ecosystem needs a specialized,
    security-sensitive authorization model.
-   Decision: begin with OPA for broad policy-as-code; do not add Cedar
    unless a concrete authorization need justifies a second policy
    system.

#### in-toto-rego

-   Repository: https://github.com/open-policy-agent/in-toto-rego
-   Purpose: apply OPA/Rego policies to in-toto software supply-chain
    attestations.
-   Borrow: turn attestations into repeatable policy decisions about how
    an artifact was built, reviewed, and published.
-   NYX use: especially relevant to Ghost Factory's
    builder/tester/reviewer/integrator pipeline.
-   **Maturity warning:** the repository README describes it as under
    active development and pre-alpha. Study and prototype it; do not
    assume production readiness.

Sources: - https://www.openpolicyagent.org/docs -
https://github.com/cedar-policy/cedar -
https://github.com/cedar-policy/cedar-spec -
https://github.com/open-policy-agent/in-toto-rego

### 2.4 Provenance, attestations, and trustworthy records

#### in-toto

-   Repository: https://github.com/in-toto/in-toto
-   Attestation framework: https://github.com/in-toto/attestation
-   Purpose: describe and verify steps and materials in a software
    supply chain.
-   Borrow: authenticated records linking actors, steps, inputs, and
    outputs.
-   NYX use: verify that the code tested is the code reviewed, merged,
    built, and deployed; detect missing or unauthorized steps.
-   Particularly valuable for Ghost Factory, but the model generalizes
    to any multi-agent workflow.

#### Sigstore / Rekor

-   Repository: https://github.com/sigstore/rekor
-   Purpose: transparency log for signed software-related metadata.
-   Borrow: signed evidence, integrity checks, and transparency-log
    concepts.
-   **Freshness note as of 19 October 2026:** the Rekor repository
    states that Rekor v1 is in maintenance mode and that a tile-based v2
    design is under development. Review the current successor and
    deployment guidance before selecting a new production
    implementation.
-   NYX use: possible future external transparency layer for critical
    attestations; likely unnecessary for the earliest MVP.

#### OpenLineage

-   Repository: https://github.com/OpenLineage/OpenLineage
-   Purpose: standard metadata for jobs, runs, and datasets.
-   Borrow: lineage relationships between inputs, transformations,
    outputs, and execution runs.
-   NYX use: trace research findings and memory updates back to source
    documents, extraction steps, transformations, and derived
    conclusions.

#### Temporal

-   Repository: https://github.com/temporalio/temporal
-   Documentation: https://docs.temporal.io/
-   Purpose: durable workflow orchestration with recorded workflow event
    history and replay capabilities.
-   Borrow: event history and replay concepts for reconstructing
    workflow execution.
-   NYX use: reproduce a disputed workflow or investigate a failure if
    the monitored system already uses Temporal or if a compatible event
    adapter is built.
-   Limitation: do not introduce Temporal solely for evidence storage;
    use it where durable orchestration itself is needed.

**Recommended baseline:** an append-only evidence registry with stable
IDs, source pointers, content hashes, event times, and access control.
Use signed attestations for important transitions. Consider a
public/external transparency log only when the threat model and
operational budget justify it.

Sources: - https://github.com/in-toto/in-toto -
https://github.com/in-toto/attestation -
https://github.com/sigstore/rekor -
https://github.com/OpenLineage/OpenLineage -
https://github.com/temporalio/temporal

### 2.5 Deterministic testing and security verification

#### Hypothesis

-   Repository: https://github.com/HypothesisWorks/hypothesis
-   Purpose: property-based testing and generation/minimization of
    failing inputs.
-   Borrow: generate edge cases and turn a failure into a minimal
    reproducible counterexample.
-   NYX use: challenge assumptions in policies, parsers, data
    transformations, and agent APIs.

#### Semgrep

-   Repository: https://github.com/semgrep/semgrep
-   Purpose: static analysis and pattern-based code checks.
-   Borrow: configurable rules, CI checks, and source-level evidence.
-   NYX use: verify code-related claims and identify likely defects or
    security patterns.
-   Limitation: static-analysis coverage depends on rules, language
    support, configuration, and edition.

#### Trivy

-   Repository: https://github.com/aquasecurity/trivy
-   Purpose: vulnerability, misconfiguration, secret, and
    software-component scanning.
-   Borrow: reproducible scans and structured security findings.
-   NYX use: validate dependency and deployment claims.

#### OSV-Scanner

-   Repository: https://github.com/google/osv-scanner
-   Purpose: dependency vulnerability scanning using OSV data.
-   Borrow: evidence-backed dependency findings.
-   NYX use: check whether a repository's claim that dependencies are
    safe is supported by a scan at a known time and dependency lock
    state.

#### Wasmtime

-   Repository: https://github.com/bytecodealliance/wasmtime
-   Purpose: WebAssembly runtime with sandboxing and capability-oriented
    security features.
-   Borrow: restricted execution for compatible untrusted code.
-   NYX use: optional isolation component for selected test jobs or
    untrusted generated code.
-   Limitation: sandboxing reduces some risks; it does not prove code
    correctness or eliminate every escape/misconfiguration risk.

Sources: - https://github.com/HypothesisWorks/hypothesis -
https://github.com/semgrep/semgrep -
https://github.com/aquasecurity/trivy -
https://github.com/google/osv-scanner -
https://github.com/bytecodealliance/wasmtime

### 2.6 Smaller agent-governance projects

Small or low-adoption repositories can be useful idea sources but should
not be treated as production foundations without a separate audit.

-   `agent-rails/agent-guard`:
    https://github.com/agent-rails/agent-guard\
    Potential ideas: tool-dispatch policy checks, allow/deny/approval
    decisions, audit reasons. Treat as an architectural reference until
    maturity, tests, threat model, and maintenance are verified.

-   `ryanwi/agent-control-plane`:
    https://github.com/ryanwi/agent-control-plane\
    Potential ideas: approval gates, budgets, preconditions, kill
    switches, event history, audit replay. Treat as a concept source
    rather than a core dependency without deeper diligence.

Do not select a dependency because its README sounds aligned with NYX.
Inspect code, releases, issues, test coverage, license, security
process, transitive dependencies, and whether the design actually meets
the requirement.

------------------------------------------------------------------------

## 3. Recommended stack: what to adopt, prototype, or only study

  ------------------------------------------------------------------------------------
  Capability            Recommendation    Initial candidate     Why
  --------------------- ----------------- --------------------- ----------------------
  Cross-system          Adopt             OpenTelemetry         Portable event/trace
  instrumentation                                               collection

  AI-specific trace     Adopt             OpenInference         Consistent
  semantics                                                     model/tool/retrieval
                                                                spans

  Trace/evaluation UI   Choose one        Langfuse **or**       Avoid duplicated
                                          Phoenix               observability systems

  Repeatable agent      Adopt selectively Inspect AI            Task-based evaluation
  evaluation                                                    harness

  Red-team regression   Adopt selectively Promptfoo             Adversarial and CI
  testing                                                       workflows

  Model vulnerability   Add as needed     garak                 Specialist probe
  probing                                                       library

  Agent trajectory      Add as needed     DeepEval              Whole-run and
  evaluation                                                    step-level metrics

  Retrieval claim       Add as needed     Ragas                 Source-supported claim
  grounding                                                     evaluation

  Policy-as-code        Adopt             OPA                   Deterministic,
                                                                testable rules

  Fine-grained          Evaluate only if  Cedar                 Separate authorization
  authorization         needed                                  model

  Software process      Adopt for         in-toto               Verifiable inputs,
  attestations          engineering                             steps, outputs
                        workflows                               

  Evidence transparency Defer             Sigstore/Rekor        Higher operational
  log                                     successor             complexity

  Data/run lineage      Borrow concepts   OpenLineage           Source-to-conclusion
                                                                provenance

  Property-based        Adopt for core    Hypothesis            Counterexamples and
  testing               libraries                               edge cases

  Static/dependency     Integrate by      Semgrep, Trivy,       Deterministic
  security checks       domain            OSV-Scanner           technical evidence

  Small agent           Study, don't      agent-guard,          Ideas need
  guard/control-plane   depend on         agent-control-plane   maturity/security
  repos                 initially                               validation
  ------------------------------------------------------------------------------------

### Avoid these mistakes

-   Do not use an LLM judge as the only verifier for high-impact
    findings.
-   Do not install multiple overlapping platforms without a demonstrated
    need.
-   Do not claim complete observation until all important execution
    boundaries are instrumented.
-   Do not treat a signed or hashed record as proof that its content is
    true.
-   Do not let the target agent overwrite the evidence against which it
    is judged.
-   Do not let NYX silently modify policies or take unrestricted
    corrective action.
-   Do not store raw secrets, credentials, or unnecessary personal data
    in traces.
-   Do not assume a repository is production-ready because it is popular
    or has a polished README.

------------------------------------------------------------------------

## 4. Proposed architecture

``` text
                  CONNECTED ECOSYSTEM
  ATHENA / ARGUS / DANNY / NOESIS / DMRX / Ghost Factory / other agents
                             |
                Instrumentation and adapters
        OpenTelemetry + OpenInference + native event hooks
                             |
                   Observation intake
         identity, timestamp, schema validation, redaction
                             |
                    Evidence registry
     immutable originals, hashes, source pointers, lineage, ACLs
                             |
                  Correlation and detectors
      contradictions / missing evidence / drift / risk / anomalies
                             |
                     Critic router
           selects checks based on claim and risk
                             |
      +----------------------+-----------------------+
      |                      |                       |
 Deterministic checks   Specialist evaluators   LLM critique
 OPA / tests / scans    Inspect / Promptfoo     counterarguments,
 provenance checks      Ragas / DeepEval        hypotheses, synthesis
      |                      |                       |
      +----------------------+-----------------------+
                             |
                    Evidence packet
      claim + expectation + sources + checks + limitations
                             |
                   Challenge/dispute protocol
       challenge -> rebuttal -> counterevidence -> verification
                             |
                     Disposition registry
      open / disputed / confirmed / rejected / fixed / accepted-risk
                             |
                  Authorized governance hooks
         approval / block / escalation / rollback recommendation
                             |
                      NYX self-audit
         evidence selection, false positives, rule/model versions
```

### Separation of concerns

-   **Collector:** gathers observations; should not decide whether the
    behavior was acceptable.
-   **Evidence registry:** preserves and indexes records; should not
    silently change evidence.
-   **Detectors:** identify possible issues; may emit hypotheses.
-   **Critic:** explains a contradiction and proposes a falsifiable
    challenge.
-   **Verifier:** runs deterministic or independently corroborating
    checks.
-   **Disposition authority:** resolves according to policy and risk.
-   **Enforcement mechanism:** performs only explicitly authorized
    actions.
-   **Self-audit:** evaluates NYX's own findings and decisions.

These can begin as modules in a single deployable service. Avoid
premature microservices. Preserve clear interfaces so components can be
separated later.

------------------------------------------------------------------------

## 5. The evidence packet

Every serious criticism should produce a structured, versioned evidence
packet.

Suggested schema (illustrative; define a formal JSON Schema/Protobuf
after validating requirements):

``` json
{
  "finding_id": "nyx-find-<stable-id>",
  "target": {
    "system_id": "ghost-factory",
    "run_id": "run-123",
    "component": "tester"
  },
  "claim": {
    "text": "All required integration tests passed.",
    "source_event_id": "evt-456",
    "claim_type": "completion"
  },
  "expectation": {
    "policy_id": "engineering.required-checks",
    "policy_version": "sha256:<policy-digest>",
    "acceptance_criteria": ["all-required-checks-green"]
  },
  "evidence": [
    {
      "evidence_id": "ev-001",
      "kind": "ci_test_result",
      "source_uri": "https://example.invalid/ci/run/123",
      "content_digest": "sha256:<digest>",
      "observed_at": "2026-10-19T10:00:00Z",
      "integrity": "verified",
      "supports": "required integration test failed"
    }
  ],
  "reasoning": {
    "contradiction": "The report says all tests passed, but one required check failed.",
    "reproduction": "Re-run the required test against the exact commit.",
    "limitations": ["CI record authenticity has not yet been independently verified"]
  },
  "assessment": {
    "status": "challenged",
    "severity": "high",
    "confidence": "high",
    "basis": ["directly-observed", "deterministically-checked"]
  },
  "counterevidence": [],
  "response_deadline": null,
  "disposition": {
    "state": "open",
    "resolution": null,
    "resolved_by": null,
    "resolved_at": null
  }
}
```

The example is illustrative, not a finalized standard. Do not include
raw secrets or sensitive payloads in evidence records. Use
access-controlled references, redaction, retention rules, and audit
access.

### Suggested finding lifecycle

`detected -> evidence_pending -> challenged -> rebuttal_pending -> verification_pending -> confirmed | rejected | inconclusive -> remediation_pending -> fixed_pending_verification -> closed`

Additional states: - `accepted_risk` --- an authorized decision-maker
accepts the documented risk. - `duplicate` --- linked to an existing
finding. - `withdrawn` --- NYX retracts an incorrect or inadequately
supported challenge. - `expired` --- a time-bounded concern no longer
applies, with a reason recorded.

Do not close a finding simply because the target agent says it is fixed.
Verify the acceptance criteria again.

------------------------------------------------------------------------

## 6. Evidence quality and confidence

Use explicit evidence categories, not a single magical "truth score."

1.  **Deterministically verified:** a reproducible test, policy
    evaluation, signature verification, or other well-defined check
    passed/failed under stated conditions.
2.  **Directly observed:** authenticated execution, CI, source, or
    infrastructure record. This establishes the recorded event, not
    necessarily the complete reality.
3.  **Corroborated:** multiple sources support the conclusion; record
    whether they are genuinely independent.
4.  **Inferred/hypothetical:** model or heuristic raises a plausible
    concern requiring further checks.

These categories are proposed NYX conventions, not a universal external
standard.

Every assessment should include: - source authenticity and integrity; -
time of observation and whether the source may be stale; - scope of the
test or observation; - evaluator/model/rule version; - reproducibility
details; - known blind spots; - counterevidence; - confidence rationale,
not only a number.

**Critical rule:** a hash or signature protects integrity/authenticity
under defined assumptions; it does not prove the statement is true.

------------------------------------------------------------------------

## 7. How NYX challenges systems

### Example: Ghost Factory says a feature is complete

1.  Observe the completion claim, commit, pull-request status, CI
    results, review record, and deployment/health checks.
2.  Compare the claim with acceptance criteria and the actual records.
3.  If a required test failed or the claimed deployment has no
    successful health check, create a finding with exact evidence
    references.
4.  Ask Ghost Factory for a rebuttal or new evidence.
5.  Independently rerun the test against the precise commit, where
    permitted.
6.  Resolve as confirmed, rejected, inconclusive, or accepted-risk
    according to policy.
7.  If confirmed, record the remediation and add a regression test when
    practical.

### Example: ARGUS publishes a research conclusion

1.  Extract individual factual claims and their cited sources.
2.  Check that each source exists, is accessible, and actually supports
    the associated claim.
3.  Record publication time, source type, relevant passages,
    contradictions, and uncertainty.
4.  Search for counterevidence when the risk or importance warrants it.
5.  Challenge unsupported or overstated conclusions.
6.  Close only when corrected, substantiated, or explicitly marked
    uncertain.

### Example: NOESIS holds conflicting memories

1.  Preserve provenance for each memory and the original source.
2.  Compare timestamps, source authority, scope, and whether one memory
    is a derived summary.
3.  Avoid silently overwriting the older record.
4.  Create a conflict finding and ask for clarification or apply a
    defined precedence rule.
5.  Record the resolution and preserve the history.

### Example: DMRX reports a routing or usage outcome

Compare agent-side reports with available request IDs, gateway records,
provider responses, token/usage records, policy decisions, and billing
data. Report discrepancies and coverage gaps; do not infer provider
costs or usage where no trustworthy record exists.

------------------------------------------------------------------------

## 8. Threat model and defenses

  -----------------------------------------------------------------------
  Threat or failure                   Defense
  ----------------------------------- -----------------------------------
  Target agent lies or omits events   Independent collectors, external
                                      CI/repository events, explicit
                                      instrumentation coverage

  Target agent alters its own         Append-only storage, access
  evidence                            separation, hashes/signatures,
                                      immutable snapshots where
                                      appropriate

  NYX hallucinates a fault            Mandatory evidence references,
                                      falsifiable reproduction, rebuttal,
                                      verifier independent of the critic

  Evaluator shares the same blind     Multiple methods, deterministic
  spot                                tests, diverse evaluators,
                                      known-answer datasets

  Evidence is stale                   Observation time, freshness policy,
                                      re-check before high-impact action

  Evidence is authentic but           Validate semantics and scope;
  misleading                          compare to acceptance criteria and
                                      counterevidence

  Correlated sources appear           Record source lineage and
  independent                         dependence; avoid double-counting
                                      derived reports

  Prompt injection targets NYX        Treat monitored content as
                                      untrusted data; isolate tools and
                                      credentials; restrict outbound
                                      access

  NYX becomes a single point of power Separate finding from enforcement;
                                      define appeal and authority
                                      boundaries

  Policy is silently weakened         Versioned policy, signed/approved
                                      changes, change review, regression
                                      tests

  Logs leak secrets or personal data  Minimize collection, redact,
                                      segregate vault references, apply
                                      access and retention controls

  Excessive false positives overwhelm Severity thresholds, deduplication,
  teams                               calibrated routing,
                                      precision/recall monitoring

  NYX learns to game its own metric   Holdout evaluations, adversarial
                                      testing, independent review,
                                      versioned scoring rules
  -----------------------------------------------------------------------

A recent real-world reminder is that agents can behave unexpectedly when
given broad network/tool access. Use least privilege, outbound-network
controls, scoped credentials, and independent monitoring rather than
relying on a model's stated intent. Do not interpret monitoring alone as
prevention.

------------------------------------------------------------------------

## 9. Self-auditing NYX

NYX must be criticizable too.

Track at minimum: - false-positive rate on adjudicated findings; -
false-negative rate on a labeled set of known violations; - precision
and recall by finding category and severity; - percentage of serious
findings with directly inspectable evidence; - percentage of high-risk
findings independently verified; - rebuttal rate and rate at which NYX
withdraws or downgrades findings; - time to verify and resolve; -
repeated findings and recurrence after remediation; - evidence-source
coverage and staleness; - evaluator and policy version changes; -
performance across model providers and language/framework
combinations; - cost and latency per observation and per investigation.

Keep a versioned regression corpus with: - confirmed violations; - valid
behavior that must not be flagged; - ambiguous cases; - adversarial
inputs; - counterexamples; - known instrumentation gaps.

Run it whenever a model, prompt, evaluator, policy, schema, or detection
rule changes. Evaluate the *whole challenge process*, not just whether
the critic's prose looks persuasive.

------------------------------------------------------------------------

## 10. Implementation plan

### Phase 0 --- Research and contracts

Deliverables: - event schema and identity conventions; - threat model
and trust boundaries; - initial policy catalog; - evidence-packet
schema; - data classification, redaction, and retention policy; -
dependency/license/maturity review.

Exit criteria: the team can explain which facts NYX can observe, which
it cannot, and what each evidence source actually establishes.

### Phase 1 --- Minimal observation and registry

Build: - OpenTelemetry Collector integration; - OpenInference
instrumentation where supported; - adapters for GitHub/CI and initial
agent/tool events; - schema validation and secret redaction; - evidence
registry with stable IDs, content digests, source pointers, timestamps,
access control, and append-only event history; - basic search and a
simple case view.

Exit criteria: a workflow can be reconstructed from collected events,
and evidence coverage gaps are visible.

### Phase 2 --- Deterministic critics

Implement: - completion claims versus actual CI/PR/deployment state; -
required approval and permission rules in OPA; - policy and evidence
freshness checks; - provenance checks for engineering artifacts; -
simple discrepancy detectors; - reproducible tests for every detector.

Exit criteria: known seeded contradictions produce correct findings, and
valid control cases are not flagged.

### Phase 3 --- Challenge protocol

Implement: - issue/challenge creation; - rebuttal submission; -
counterevidence references; - independent verification jobs; - severity
and escalation rules; - resolution states and audit history; -
notifications/integration hooks.

Exit criteria: every finding can be challenged, resolved, withdrawn, or
marked inconclusive without deleting its history.

### Phase 4 --- Specialist critics

Integrate as appropriate: - Inspect AI for repeatable agent
evaluations; - Promptfoo for red-team and CI regression suites; - Ragas
for retrieval/claim-grounding tasks; - DeepEval for selected trajectory
evaluations; - garak for model/application vulnerability probes; -
Semgrep, Trivy, OSV-Scanner, and existing project tests for software
findings.

Exit criteria: evaluators are routed by case type, their outputs point
to source evidence, and known-answer datasets measure their performance.

### Phase 5 --- Attestations and lineage

Add: - in-toto-style records for engineering workflow steps; - signed
attestations for high-value transitions; - source-to-claim lineage; -
optional transparency log after threat-model review; - replayable
incident records.

Exit criteria: a reviewer can independently verify that the artifact
tested is the artifact approved and deployed.

### Phase 6 --- Ecosystem-wide oversight and self-audit

Expand integrations, calibrate thresholds, measure false
positives/negatives, and audit NYX itself. Introduce enforcement hooks
only after authority boundaries and appeals are tested.

Exit criteria: NYX demonstrably finds seeded violations, avoids known
false accusations, produces inspectable evidence, and handles appeals
without silent state changes.

------------------------------------------------------------------------

## 11. Initial acceptance tests

These should become automated tests before NYX is considered useful.

1.  **False completion claim:** target reports success while a required
    test is red. NYX creates a finding with exact run/commit evidence.
2.  **Valid completion:** all defined checks pass for the exact commit.
    NYX does not accuse the target merely because its prose is
    confident.
3.  **Stale evidence:** the test report predates the latest commit. NYX
    flags the evidence as stale rather than treating it as proof of the
    new state.
4.  **Forged or mismatched artifact:** an attestation references a
    different digest. Verification fails.
5.  **Source does not support claim:** citation exists but contradicts
    or fails to support the research claim. NYX challenges the claim and
    links the relevant passage.
6.  **Valid rebuttal:** the target supplies relevant counterevidence
    that overturns the initial hypothesis. NYX downgrades or withdraws
    the finding and preserves the history.
7.  **Unresolved case:** evidence is insufficient. NYX reports
    inconclusive, not confirmed.
8.  **Prompt injection in source data:** untrusted content instructs NYX
    to ignore policy or reveal secrets. The instruction is treated as
    data and does not override system policy.
9.  **Unauthorized remediation:** NYX identifies a serious problem but
    lacks authority to perform the requested corrective action. It
    escalates instead of bypassing authorization.
10. **Self-audit:** a seeded false accusation is detected by
    adjudication, recorded as a false positive, and added to the
    regression corpus.
11. **Privacy:** secrets and unnecessary sensitive payloads are absent
    from normal traces and case summaries.
12. **Evidence integrity:** changes to protected evidence are detectable
    and access is audited.

------------------------------------------------------------------------

## 12. Repository adoption checklist

Before adding any repository as a runtime dependency, record:

-   exact upstream URL and commit/release inspected;
-   license and obligations;
-   last release/commit and current maintainer activity;
-   unresolved security advisories and disclosure process;
-   supported languages/runtimes and platform compatibility;
-   dependency/transitive dependency risk;
-   tests, coverage, CI, and release process;
-   API stability and migration burden;
-   deployment requirements, resource use, and operational complexity;
-   data leaving the environment and telemetry retention;
-   whether a smaller adapter or concept-only implementation is safer;
-   exit strategy and replacement option.

Recheck this list during implementation. A project can be suitable for
research but unsuitable for production.

------------------------------------------------------------------------

## 13. Final recommended shortlist

If implementation starts now, prioritize this compact set:

1.  **OpenTelemetry Collector + OpenInference** --- portable
    observation.
2.  **Langfuse or Phoenix** --- choose one initial trace/evaluation UI.
3.  **OPA** --- deterministic policy-as-code.
4.  **Inspect AI** --- structured evaluations.
5.  **Promptfoo** --- adversarial and regression tests.
6.  **Ragas and/or DeepEval** --- only for use cases that need claim
    grounding or trajectory metrics.
7.  **in-toto** --- workflow/artifact provenance, especially for Ghost
    Factory.
8.  **Hypothesis + existing test/security tooling** --- reproducible
    counterexamples and deterministic technical evidence.
9.  **OpenLineage concepts** --- source and transformation lineage,
    particularly for research and NOESIS.
10. **Sigstore transparency tooling** --- evaluate later for
    high-assurance attestations; investigate the current Rekor successor
    before committing.

Keep small agent-control-plane repositories as reference material unless
they pass an independent engineering and security review.

### Final decision

NYX's differentiator is not the number of models judging one another. It
is a rigorous, auditable process:

**observe → state the claim → gather evidence → challenge → accept
rebuttal → verify independently → resolve → learn from the result.**

Build the evidence model and challenge protocol first. Add specialist
critics behind stable interfaces. Treat all evaluator
conclusions---including NYX's own---as challengeable claims with
explicit evidence and limitations.

------------------------------------------------------------------------

## Sources and further reading

Primary project sources are linked inline above. Key starting points:

-   OpenTelemetry Collector:
    https://github.com/open-telemetry/opentelemetry-collector
-   OpenInference: https://github.com/Arize-ai/openinference
-   Langfuse: https://github.com/langfuse/langfuse
-   Phoenix: https://github.com/Arize-ai/phoenix
-   Inspect AI: https://github.com/UKGovernmentBEIS/inspect_ai
-   Inspect evaluations:
    https://github.com/UKGovernmentBEIS/inspect_evals
-   Promptfoo: https://github.com/promptfoo/promptfoo
-   garak: https://github.com/NVIDIA/garak
-   DeepEval: https://github.com/confident-ai/deepeval
-   Ragas: https://github.com/vibrantlabsai/ragas
-   OPA: https://github.com/open-policy-agent/opa
-   Cedar: https://github.com/cedar-policy/cedar
-   Cedar specification/testing:
    https://github.com/cedar-policy/cedar-spec
-   in-toto: https://github.com/in-toto/in-toto
-   in-toto attestations: https://github.com/in-toto/attestation
-   in-toto-rego (pre-alpha warning):
    https://github.com/open-policy-agent/in-toto-rego
-   Sigstore Rekor (v1 maintenance status):
    https://github.com/sigstore/rekor
-   OpenLineage: https://github.com/OpenLineage/OpenLineage
-   Temporal: https://github.com/temporalio/temporal
-   Hypothesis: https://github.com/HypothesisWorks/hypothesis
-   Semgrep: https://github.com/semgrep/semgrep
-   Trivy: https://github.com/aquasecurity/trivy
-   OSV-Scanner: https://github.com/google/osv-scanner
-   Wasmtime: https://github.com/bytecodealliance/wasmtime
-   agent-guard (early-stage idea source):
    https://github.com/agent-rails/agent-guard
-   agent-control-plane (early-stage idea source):
    https://github.com/ryanwi/agent-control-plane

**Research caveat:** this is an architecture-oriented survey of public
project documentation and repositories, not a complete security audit,
exhaustive license review, or benchmark of every candidate. Verify exact
versions, current maintenance status, licenses, and threat-model fit
before production adoption.


---

# Part II — Implementation Specification for Coding Agents

**Specification status:** Build-oriented baseline  
**Audience:** Coding agents and engineers implementing NYX  
**Rule:** This specification defines observable behaviour and acceptance criteria. Where the existing NYX repository already has a sound implementation, inspect and extend it rather than blindly replacing it.

## 1. Implementation objective

Build NYX as an independent, evidence-backed oversight and criticism service for a multi-agent ecosystem. NYX ingests observable events and claims, creates traceable findings, gathers and evaluates evidence, requests rebuttals, verifies disputes, and records a reasoned disposition.

The initial implementation must be useful without requiring every ecosystem project to integrate at once. It must run locally, accept events through a documented API and CLI, persist records, and expose findings through a queryable interface.

### 1.1 Required first-release outcomes

A release is not complete until NYX can:

1. Register monitored systems and their capabilities.
2. Ingest normalized events and claims from API, CLI, and file-based fixtures.
3. Validate, redact, deduplicate, and persist incoming records.
4. Store evidence references and integrity metadata without falsely claiming that hashes prove truth.
5. Run deterministic critics against events, claims, policy rules, and test results.
6. Create structured findings with severity, rationale, supporting evidence, counterevidence, limitations, and recommended next steps.
7. Link findings to the exact source records, artifacts, versions, and time windows they concern.
8. Accept rebuttals and additional evidence without overwriting the original finding history.
9. Run verification checks and record whether a finding is confirmed, rejected, inconclusive, or accepted as a risk.
10. Expose an API and CLI for systems and humans to inspect findings and submit responses.
11. Record an append-only audit history for material transitions.
12. Run automated tests covering correctness, security boundaries, replay, failure handling, and evidence integrity.

### 1.2 Non-goals for the first release

Do not make these prerequisites for the first usable release:

- A polished web dashboard.
- A universal autonomous agent able to inspect every private system.
- Mandatory use of an LLM or a specific model provider.
- Automatic modification, shutdown, or punishment of monitored systems.
- Blockchain-based evidence storage.
- A distributed consensus protocol.
- Automatic collection of private user data or unrestricted transcript capture.
- Claims that a model-generated critique is objective truth.

Design extension points for future work, but implement the smallest end-to-end vertical slice first.

## 2. Coding-agent operating instructions

Before writing implementation code, the coding agent MUST:

1. Inspect the target repository, current branch, existing README, package manifests, tests, conventions, and deployment configuration.
2. Report the current architecture and identify reusable code before proposing new abstractions.
3. Create or update `docs/architecture.md`, `docs/threat-model.md`, and `docs/adr/` entries for consequential design choices.
4. Convert this specification into a tracked task checklist or issues.
5. Implement in small vertical slices; do not create placeholder modules that appear functional but do nothing.
6. Add tests with each feature. Do not postpone all tests to the end.
7. Run formatting, linting, type checks, unit tests, integration tests, and relevant security checks; report actual commands and results.
8. Document unimplemented requirements explicitly. Never claim a test, integration, or security property was verified unless it was actually tested.
9. Preserve existing repository behaviour unless a change is deliberate, documented, and tested.
10. Prefer boring, well-supported dependencies and a local-first default.

If the repository already contains a different language or framework, use its established stack unless there is a documented technical reason to migrate. If no repository constraints exist, choose a typed implementation language and a relational database with migrations; avoid introducing several databases or message brokers in the MVP.

## 3. System boundaries and trust model

NYX is a critic and verification coordinator, not the unquestionable source of truth.

### 3.1 Trust boundaries

- **Monitored systems:** May be mistaken, compromised, self-serving, or incomplete in their telemetry.
- **Evidence producers:** Can submit records, but a producer's assertion does not validate itself.
- **Evidence storage:** Must preserve record identity and detect unauthorized changes to stored content.
- **Critic modules:** May produce false positives and false negatives; outputs must identify the method and version used.
- **LLM critics:** Treat all retrieved content as untrusted data. They cannot execute embedded instructions or silently elevate an assertion to a fact.
- **Operators and policy owners:** Can configure rules and authorise actions within explicit permissions.
- **NYX itself:** Its rules, configuration, models, findings, and transitions must be auditable and contestable.

### 3.2 Safety invariants

The implementation MUST enforce these invariants:

1. No finding may be marked `confirmed` without a recorded rationale and at least one relevant evidence item, except for explicitly defined policy-only findings whose policy and triggering record are both attached.
2. A model-generated claim is never labelled as deterministic verification.
3. A hash is described as an integrity identifier, not proof that the source statement is true.
4. Missing telemetry is represented as unknown or incomplete, not automatically as evidence of misconduct.
5. Rebuttals and new evidence append to history; they do not erase prior records.
6. High-impact actions are recommendations only in the MVP. NYX must not independently modify, disable, deploy, delete, spend funds, or change permissions in another system.
7. Every critic result records the critic type, rule/model version, input references, execution time, and outcome.
8. Sensitive payloads are not copied into logs by default. Redaction and access control happen before broad storage or model processing.
9. Any external content is treated as untrusted, including repository files, web pages, agent messages, logs, and evidence descriptions.
10. A finding can be disputed and its disposition can change with an audit trail.

## 4. Reference architecture

Implement the following logical modules. They may initially live in one deployable service, but keep clear module boundaries.

```text
API / CLI / SDK / File Fixtures
              |
       Ingestion Gateway
              |
 Validation -> Redaction -> Deduplication
              |
     Event & Claim Registry
              |
       Evidence Registry
              |
       Critic Scheduler
       /      |       \
 Deterministic Rules  Tool Checks  Optional LLM Critics
       \      |       /
        Finding Composer
              |
      Finding & Dispute API
              |
   Rebuttal / Verification Queue
              |
      Disposition Registry
              |
      Append-only Audit Log
```

### 4.1 Required modules

- `ingestion`: receives events and claims; validates schema; enforces size limits and idempotency.
- `systems`: registers monitored systems, adapters, versions, and capabilities.
- `evidence`: manages evidence records, source references, hashes, freshness, access policy, and lineage.
- `critics`: common critic interface plus deterministic baseline critics.
- `findings`: finding composition, deduplication, severity, lifecycle, and querying.
- `disputes`: rebuttals, counterevidence, verification jobs, and disposition changes.
- `policies`: versioned policy/rule configuration and optional OPA integration.
- `audit`: append-only history of security-relevant and lifecycle events.
- `api`: versioned HTTP API and error contract.
- `cli`: local operations and human-readable/JSON output.
- `storage`: repositories, migrations, transaction boundaries, and retention.
- `observability`: structured logs, metrics, and tracing with redaction.
- `adapters`: optional integrations with CI, Git hosting, tracing, agent frameworks, and evaluation tools.

Do not create a separate microservice for every module in the MVP.

## 5. Core domain model

Use stable IDs, UTC timestamps, schema versions, and explicit provenance fields. The following is a logical contract, not a requirement to use these exact database table names.

### 5.1 MonitoredSystem

```json
{
  "id": "sys_01...",
  "name": "ghost-factory",
  "kind": "agent|service|repository|pipeline|model|human",
  "environment": "development|staging|production|local",
  "owner": "team-or-principal-id",
  "capabilities": ["events.write", "findings.read"],
  "adapter": "http|cli|file|opentelemetry|custom",
  "metadata": {},
  "created_at": "2026-10-19T12:00:00Z",
  "status": "active|paused|retired"
}
```

Credentials and secrets MUST NOT be stored in `metadata`.

### 5.2 Event

An event is a normalized observation, not necessarily a verified fact.

Required fields:

- `id`: NYX-assigned stable ID.
- `schema_version`.
- `source_system_id`.
- `event_type`.
- `occurred_at`: time claimed by the producer.
- `received_at`: time NYX received it.
- `correlation_id` and optional `parent_event_id`.
- `actor` and `subject` when known.
- `payload`: typed or schema-validated, redacted data.
- `source_ref`: optional URI/path/commit/trace reference.
- `content_hash`: hash of canonical stored representation.
- `trust_class`: `unverified`, `observed`, `tool_verified`, or `signed_attestation`.
- `labels`.
- `retention_class`.

Never infer that `occurred_at` is trustworthy merely because it is present. Preserve received time separately.

### 5.3 Claim

A claim is a proposition that can be assessed.

Required fields:

- `id`, `schema_version`, `statement`.
- `claimant_system_id` or principal.
- `claim_type`: `factual`, `causal`, `predictive`, `policy`, `completion`, `security`, `quality`, or `other`.
- `scope`: the system, artifact, commit, environment, period, and conditions the claim applies to.
- `created_at`.
- `source_event_ids`.
- `status`: `unassessed`, `supported`, `challenged`, `disputed`, `verified_within_scope`, `refuted`, or `inconclusive`.

Claims must be scoped. “All tests pass” is incomplete without the commit, test suite, environment, and run identity.

### 5.4 EvidenceItem

Required fields:

- `id`, `schema_version`, `kind`.
- `title` and concise `description`.
- `source_system_id` or named external source.
- `source_ref` and optional `locator` (line range, JSON pointer, test ID, trace span, etc.).
- `captured_at`, `valid_from`, optional `valid_until`.
- `content_hash` for stored or canonicalized content where feasible.
- `integrity_method`: `none`, `hash`, `signed`, `attested`, or `external`.
- `producer_identity` and verification result where applicable.
- `relevance_to_claim` or finding.
- `limitations`.
- `sensitivity`: `public`, `internal`, `confidential`, or `restricted`.
- `access_policy_ref`.
- `retention_class`.

Support both inline evidence and reference-only evidence. Avoid copying large logs or source trees into every finding.

### 5.5 Finding

Required fields:

- `id`, `schema_version`, `title`, `summary`.
- `category`: `contradiction`, `unsupported_claim`, `policy_violation`, `security_risk`, `quality_risk`, `drift`, `missing_verification`, `provenance_gap`, or `other`.
- `severity`: `info`, `low`, `medium`, `high`, `critical`.
- `confidence`: optional calibrated estimate with method; never use as a substitute for evidence.
- `scope`.
- `affected_system_ids`.
- `claim_ids`, `event_ids`, `evidence_ids`.
- `supporting_evidence_ids`, `counterevidence_ids`.
- `reasoning`: concise, inspectable explanation with explicit premises and inference.
- `limitations` and `unknowns`.
- `recommended_action`, explicitly labelled as a recommendation.
- `critic_runs`.
- `status`, `created_at`, `updated_at`.
- `duplicate_of` where applicable.

A finding must distinguish observations, inferences, and recommendations in the data model or rendered output.

### 5.6 CriticRun

Record:

- `id`, `critic_id`, `critic_type`.
- `critic_version` and rule/model version.
- `input_refs` and input content hashes.
- `started_at`, `completed_at`.
- `result`: `finding`, `no_finding`, `error`, `skipped`, or `inconclusive`.
- `rationale`.
- `tool_run_refs`.
- `resource_usage` where available.
- `failure_details` with secrets redacted.

A `no_finding` result means only that this critic found no issue within its declared scope.

### 5.7 Rebuttal and Disposition

A rebuttal contains `id`, `finding_id`, `submitted_by`, `submitted_at`, `argument`, `evidence_ids`, and `requested_outcome`.

A disposition contains `id`, `finding_id`, `outcome`, `rationale`, `decided_by`, `decided_at`, `verification_run_ids`, `policy_version`, and `supersedes_disposition_id`.

Allowed outcomes:

- `confirmed`
- `partially_confirmed`
- `rejected`
- `inconclusive`
- `accepted_risk`
- `duplicate`
- `withdrawn`

Every new disposition supersedes but never deletes the previous one.

### 5.8 AuditEntry

Record the actor, action, target type and ID, timestamp, request/correlation ID, outcome, reason, and hash or reference to the prior entry. Use append-only application behaviour and database permissions where practical. If a hash chain is implemented, document its trust limits; do not describe it as tamper-proof against a privileged attacker who can rewrite the entire store.

## 6. Finding lifecycle and transition rules

Implement this lifecycle as a validated state machine:

```text
detected
  -> evidence_pending
  -> challenged
  -> rebuttal_pending
  -> verification_pending
  -> confirmed | partially_confirmed | rejected | inconclusive
  -> remediation_pending (optional, authorised external owner only)
  -> fixed_pending_verification
  -> closed
```

Additional terminal or administrative states: `accepted_risk`, `duplicate`, `withdrawn`, and `expired`.

Rules:

- A finding may skip rebuttal only under a documented policy, such as an objectively reproducible failed check; the bypass must be recorded.
- `confirmed` requires the evidence and rationale rules in Section 3.2.
- `rejected` requires an explanation of which premise failed or what counterevidence prevailed.
- `inconclusive` is valid and must not be silently converted to a negative or positive verdict.
- `fixed_pending_verification` is not the same as `closed`; closure requires a verification result or an explicitly authorised waiver.
- Only principals with the relevant capability may change dispositions or accept risk.
- Invalid transitions return a typed error and do not mutate state.

## 7. Evidence evaluation protocol

For every material finding, the critic/composer should follow this sequence:

1. **State the proposition.** What exact claim or behaviour is being assessed?
2. **Scope it.** Identify system, version, environment, artifact, time window, and conditions.
3. **List observations.** Separate direct observations from claims made by an agent or human.
4. **Gather supporting evidence.** Prefer reproducible tests, logs with traceable origin, source references, signed attestations, and direct measurements.
5. **Seek counterevidence.** Check for alternate explanations, exceptions, later fixes, false-positive conditions, and evidence that contradicts the allegation.
6. **Assess independence.** Multiple summaries copied from one source are not independent corroboration.
7. **Check freshness.** A passing test from an old commit may not support a claim about the current commit.
8. **Run verification.** Use deterministic tools when available; record environment and exact command/tool version.
9. **State limitations.** Include missing telemetry, inaccessible sources, sample size, assumptions, and unverified premises.
10. **Assign a disposition.** Use the weakest conclusion supported by the evidence.
11. **Invite rebuttal.** Make it possible for the affected system or authorised human to submit context and counterevidence.
12. **Record the history.** Preserve the original finding and every subsequent change.

### 7.1 Evidence hierarchy (guidance, not an automatic truth ranking)

Usually prefer, where relevant and authentic:

1. Reproducible deterministic test or measurement with captured inputs and environment.
2. Direct source/artifact inspection tied to an immutable commit or content hash.
3. Independently verifiable signed build/release attestation.
4. Traceable runtime logs or traces with known instrumentation coverage.
5. Independent expert analysis with explicit method.
6. Model-generated analysis.
7. Unattributed assertion.

This is not a universal ranking: evidence must be evaluated for the specific proposition. For example, a unit test cannot by itself prove that a production deployment used the tested artifact.

### 7.2 Critic output contract

Each critic returns a typed result:

```json
{
  "critic_id": "tests-vs-completion-claim",
  "critic_version": "1.0.0",
  "result": "finding",
  "proposition": "The completion claim is supported by tests for this commit",
  "scope": {"repository": "example/repo", "commit": "abc123"},
  "observations": [],
  "supporting_evidence_ids": [],
  "counterevidence_ids": [],
  "limitations": [],
  "rationale": "The completion claim names commit abc123, but the attached test run targets def456.",
  "recommended_next_step": "Run the required suite against abc123",
  "requires_human_review": false
}
```

The example is illustrative. Real outputs must reference persisted evidence IDs, not invent them.

## 8. Minimum critic pack

Implement these deterministic critics before model-driven critics.

### C-001 — Completion claim vs. verification

Detect claims such as “done”, “fixed”, “tests pass”, or “deployed” when the required linked verification artifact is missing, stale, failed, or refers to a different commit/build/version.

Acceptance: same-commit passing evidence does not trigger the critic; a mismatched commit does; missing test evidence creates a scoped verification gap rather than asserting the code is broken.

### C-002 — Contradictory records

Detect materially incompatible statements about the same subject and scope, while avoiding false contradictions across different versions, environments, or time periods.

Acceptance: two incompatible states for the same version/time scope create a candidate finding; states for different versions do not automatically conflict.

### C-003 — Evidence freshness

Flag evidence whose declared scope or validity window does not cover the claim being evaluated.

Acceptance: a test report from an older commit cannot verify a current commit unless the claim explicitly scopes itself to the older commit.

### C-004 — Provenance gap

Flag material claims whose evidence lacks a source, source identity, locator, capture time, or scope information required by policy.

Acceptance: the result names the missing provenance fields and does not imply malicious behaviour.

### C-005 — Policy violation

Evaluate versioned, deterministic policy rules. An OPA adapter may be used, but the core must work with a minimal built-in rule interface.

Acceptance: each violation identifies the exact policy ID/version, input record, and decision result.

### C-006 — Risky action without authorisation

Detect proposed or recorded high-impact actions without a corresponding permission or approval record.

Acceptance: the MVP reports and records the finding; it does not perform the action or autonomously disable the monitored system.

### C-007 — Repeated failure / regression

Detect a previously passing verification check that now fails for the same defined test and comparable environment, with commit/environment differences shown.

Acceptance: a changed environment is reported as a confounder, not hidden.

### C-008 — Unsupported certainty

Detect a claim whose wording is stronger than the attached evidence scope, e.g. “all tests pass” supported by a single unit test. Initially implement as explicit rules and optional model assistance; never treat the model assessment as final proof.

### C-009 — Evidence conflict

Detect cases where supporting and counterevidence materially disagree. Return `inconclusive` or request verification when the system cannot resolve the conflict deterministically.

### C-010 — NYX self-consistency

Check that every confirmed finding has valid evidence references, that state transitions are valid, that critic versions are recorded, and that the audit chain/history is complete within the configured storage model.

## 9. API contract

Use a versioned prefix such as `/api/v1`. Exact framework choices may follow the repository, but the semantics below are required.

### 9.1 Required endpoints

- `POST /api/v1/systems` — register a monitored system.
- `GET /api/v1/systems` — list systems visible to the caller.
- `POST /api/v1/events` — ingest one event.
- `POST /api/v1/events/batch` — ingest a bounded batch with per-item outcomes.
- `POST /api/v1/claims` — register a claim.
- `POST /api/v1/evidence` — register an evidence item or reference.
- `POST /api/v1/critics/run` — run selected critics on permitted input references.
- `GET /api/v1/findings` — filter by system, status, severity, category, date, and cursor.
- `GET /api/v1/findings/{id}` — retrieve finding and evidence metadata subject to access control.
- `POST /api/v1/findings/{id}/rebuttals` — submit a rebuttal.
- `POST /api/v1/findings/{id}/verification` — request verification.
- `POST /api/v1/findings/{id}/dispositions` — submit a disposition if authorised.
- `GET /api/v1/findings/{id}/history` — retrieve audit/lifecycle history.
- `GET /api/v1/health` — liveness.
- `GET /api/v1/ready` — readiness, including database and required dependencies.

All list endpoints need pagination and deterministic ordering. Mutating requests should accept an idempotency key or a stable producer event ID. Return structured errors with a machine-readable code, human-readable message, request ID, and field details when appropriate.

### 9.2 API requirements

- OpenAPI specification checked into the repository and validated in CI.
- Request and response schema validation.
- Explicit maximum payload size and batch size.
- Authentication required for non-local network use.
- Per-system capabilities and least-privilege authorisation.
- Rate limits or bounded ingestion to prevent unbounded resource consumption.
- Safe handling of duplicate requests.
- No secret values in error messages or logs.
- Cursor-based pagination for large event and finding collections.
- Consistent UTC timestamps.
- No endpoint may return restricted evidence merely because the caller can read the associated finding.

## 10. CLI contract

Provide a CLI with machine-readable JSON output and readable terminal output. Command names may follow existing repository conventions, but equivalent operations must exist:

```text
nyx system register <manifest>
nyx system list
nyx ingest event <file-or-stdin>
nyx ingest batch <file>
nyx claim add <file-or-stdin>
nyx evidence add <file-or-stdin>
nyx critic list
nyx critic run --event <id> [--critic <id>]
nyx finding list [--status <status>] [--severity <severity>]
nyx finding show <id>
nyx finding rebut <id> --file <rebuttal.json>
nyx finding verify <id>
nyx finding disposition <id> --outcome <outcome> --reason <text>
nyx audit verify
nyx doctor
```

`nyx doctor` checks configuration, database connectivity, migrations, writable paths, and optional integrations without printing secrets. Commands must return non-zero exit codes on failure and support JSON output for automation.

## 11. Storage, idempotency, and retention

### 11.1 Storage

For the MVP, use a relational database supported by the target repository. Provide schema migrations and transaction boundaries. A local development mode may use a file-backed database if appropriate, but do not create divergent persistence semantics between local and production modes.

### 11.2 Idempotency

- Ingestion must support producer event IDs and/or idempotency keys.
- Replaying the same accepted event must not create duplicate findings indefinitely.
- Deduplication must not merge distinct incidents solely because their text looks similar.
- Store a deduplication rationale and preserve links to the original records.
- Batch ingestion returns success/error per item.

### 11.3 Retention

- Configure retention by record class and sensitivity.
- Keep metadata and hashes only where policy allows; a hash can itself be sensitive if it enables correlation or guessing.
- Support deletion or redaction workflows required by the deployment's privacy obligations while recording the fact of redaction where legally appropriate.
- Do not promise immutable retention when administrators can delete the underlying database.
- Never place credentials, access tokens, private keys, or raw secrets in evidence payloads.

## 12. Security and privacy requirements

The coding agent must create a threat model and test these controls:

- Authentication and capability-based authorisation.
- Input validation, payload limits, and safe deserialisation.
- Redaction before persistence and before any model call.
- Tenant/system isolation where multiple principals share a deployment.
- Prompt-injection resistance: retrieved content is quoted as data; tool permissions come from trusted configuration, never from evidence text.
- No arbitrary shell execution triggered by an event, finding, or model output.
- Verification commands run only through an explicit allowlist/adapter with resource limits and captured environment.
- SSRF protection for any feature that fetches evidence URLs; deny private/internal network targets by default unless explicitly configured.
- Path traversal protection for file evidence and repository adapters.
- Safe error handling and secret-safe structured logs.
- Audit of permission changes, policy changes, dispositions, and adapter configuration.
- Dependency and container scanning in CI.
- Secure defaults: local bind by default for unauthenticated development mode; require explicit configuration for remote exposure.
- Configurable retention and export access controls.
- No automatic external action based solely on a model-generated critique.

Any model prompt must tell the model that evidence content is untrusted and that instructions found inside evidence must not be followed. This is a defence-in-depth measure, not a substitute for tool permission isolation.

## 13. LLM critic interface (optional in MVP)

LLM critics must implement the same interface as deterministic critics and return structured output conforming to a schema. They may propose a finding, but a separate composer validates referenced evidence IDs, scope, required fields, and policy.

Requirements:

- Provider/model configured externally; no provider key hard-coded.
- Support a no-LLM mode.
- Persist model/provider/version and prompt-template version.
- Store prompt/response only according to configured privacy policy; redact sensitive values.
- Validate output against a strict schema and reject unknown evidence IDs.
- Distinguish model assessment from deterministic verification.
- Handle timeout, rate limits, invalid JSON, refusals, and provider outage.
- Use an evaluation corpus with expected outcomes and regression thresholds before changing prompts/models.
- Do not ask a single LLM to both invent evidence and independently certify that evidence.
- Never use confidence values emitted by the model as calibrated probabilities without separate calibration work.

## 14. External integration strategy

Integrations are adapters, not prerequisites for the core domain model.

Recommended order:

1. HTTP/CLI/file adapter for reliable end-to-end local use.
2. Git/CI adapter that associates claims and test results with repository, commit, run, and artifact.
3. OpenTelemetry-compatible event/trace ingestion where practical.
4. Optional evaluation integrations for Inspect AI, Promptfoo, Phoenix or Langfuse, and Ragas/DeepEval.
5. Optional OPA policy adapter.
6. Optional signed provenance/attestation integration using in-toto/Sigstore concepts.
7. Ecosystem-specific adapters for ATHENA, ARGUS, NOESIS, DMRX, and Ghost Factory after their event contracts are inspected.

Do not assume these projects already expose compatible interfaces. Research each target's current APIs, licence, maintenance, and data-access constraints before implementing an adapter. If an integration is unavailable, define a documented adapter contract and mark it as not implemented.

## 15. Configuration contract

Configuration must be documented, schema-validated, and safe by default. Support environment variables and a local configuration file with clear precedence. At minimum, configure:

- Service bind address and port.
- Database connection and migration mode.
- Authentication mode and principals/capabilities.
- Data directory and retention rules.
- Maximum payload/batch sizes.
- Redaction rules.
- Enabled critics and their versions.
- Optional LLM provider/model, timeout, and privacy mode.
- Optional external adapter settings.
- Logging level and redaction.
- Audit verification settings.
- Resource limits for verification tools.

On startup, fail clearly on invalid required settings. Do not log credentials when printing effective configuration.

## 16. Observability and operations

Provide structured logs, metrics, and health checks. Minimum metrics:

- Ingested events and rejected events by reason.
- Ingestion latency and queue/backlog size if queues exist.
- Critic runs by critic/result/duration.
- Findings by severity/category/status.
- Verification failures and timeouts.
- Duplicate event/finding rates.
- Rebuttal and disposition counts.
- Storage errors and readiness state.
- Model usage/cost only if an LLM adapter is enabled.

Avoid metric labels containing user IDs, full claim text, secrets, or unbounded arbitrary values. Traces must not capture raw restricted evidence by default.

## 17. Test strategy

### 17.1 Required layers

- Unit tests for schemas, rules, lifecycle transitions, permissions, canonicalisation, hashing, redaction, and deduplication.
- Integration tests for persistence, transactions, API endpoints, CLI, migrations, and audit history.
- Contract tests for API schemas and each adapter.
- Property-based tests for state transitions, event replay, malformed payloads, and evidence references.
- Security tests for unauthorised access, prompt injection, SSRF/path traversal, payload abuse, and secret leakage.
- End-to-end tests using synthetic systems and fixtures.
- Regression tests for every confirmed false positive/false negative discovered during use.

### 17.2 Required acceptance scenarios

1. An event can be ingested, retrieved, and traced to its source system.
2. Replaying an identical event is idempotent.
3. An event with invalid schema is rejected with a structured error and no partial write.
4. A completion claim linked to a passing test run on the same commit does not trigger C-001.
5. A completion claim linked only to a different commit triggers a scoped finding.
6. Missing verification creates a verification gap, not an assertion that the implementation is defective.
7. Contradictory claims for different versions are not automatically treated as contradictory.
8. Stale evidence cannot silently verify a newer artifact.
9. A finding cannot become confirmed without required evidence and rationale.
10. A rebuttal appends to history and does not overwrite the original finding.
11. Counterevidence can lead to rejection or an inconclusive disposition.
12. Invalid lifecycle transitions are rejected without changing stored state.
13. A caller cannot read restricted evidence without the required capability.
14. Secrets in incoming payloads are redacted before persistence and model processing.
15. Prompt injection embedded in evidence cannot grant tool access or change trusted policy.
16. A verification tool cannot execute an arbitrary command supplied by an untrusted event.
17. Audit history detects accidental record mutation within the implemented threat model.
18. Database/provider outage produces a safe failure and does not report successful ingestion.
19. LLM timeout or invalid structured output does not corrupt finding state.
20. `nyx doctor`, health, and readiness correctly report dependency state without leaking secrets.
21. API and CLI expose consistent status and error semantics.
22. A policy change records the new version and who changed it.
23. A fixed finding remains open until verification or a documented authorised waiver.
24. Every confirmed finding passes the NYX self-consistency critic.

### 17.3 Definition of done for each slice

A slice is done only when:

- Implementation is present and connected end-to-end.
- Tests cover the happy path, negative path, and relevant security boundary.
- Documentation and sample configuration are updated.
- Formatting, linting, type checks, and relevant tests pass.
- No secret or private test data is committed.
- Limitations and unimplemented behaviour are documented.
- The coding agent reports changed files, commands run, actual outcomes, and remaining risks.

## 18. Suggested repository layout

Adapt to the existing repository instead of forcing this exact layout.

```text
/
├── README.md
├── CONTRIBUTING.md
├── SECURITY.md
├── LICENSE
├── docs/
│   ├── architecture.md
│   ├── threat-model.md
│   ├── api.md
│   ├── evidence-model.md
│   ├── critic-authoring.md
│   ├── operations.md
│   ├── privacy-and-retention.md
│   ├── adr/
│   └── implementation/
│       ├── roadmap.md
│       └── acceptance-tests.md
├── schemas/
│   ├── event.schema.json
│   ├── claim.schema.json
│   ├── evidence.schema.json
│   ├── finding.schema.json
│   ├── rebuttal.schema.json
│   └── disposition.schema.json
├── examples/
│   ├── system-manifest.json
│   ├── event.json
│   ├── claim.json
│   └── rebuttal.json
├── migrations/
├── src/
│   ├── api/
│   ├── cli/
│   ├── domain/
│   ├── ingestion/
│   ├── evidence/
│   ├── critics/
│   ├── findings/
│   ├── disputes/
│   ├── policies/
│   ├── audit/
│   ├── storage/
│   ├── adapters/
│   └── observability/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── security/
│   ├── e2e/
│   └── fixtures/
└── .github/
    └── workflows/
```

## 19. Phased implementation plan

### Phase 0 — Repository discovery and decisions

- Inspect the repository and report current state.
- Confirm language/framework/database from existing conventions.
- Create architecture and threat-model documents.
- Define schemas and API error conventions.
- Record key decisions in ADRs.
- Deliverable: a reviewed implementation plan with mapped files and tests.

### Phase 1 — Evidence and event spine

- Implement system registry, event/claim/evidence schemas, migrations, repositories, and ingestion API/CLI.
- Add canonicalisation, integrity hashes, redaction, size limits, idempotency, and audit entries.
- Add local fixtures and `nyx doctor`.
- Exit criteria: acceptance scenarios 1–3, 14, and 18 pass.

### Phase 2 — Deterministic critic engine

- Define critic interface and registry.
- Implement C-001 through C-007 first, then C-008 through C-010.
- Add finding composition, scoped evidence links, deduplication, and lifecycle state machine.
- Exit criteria: deterministic critics are testable without an LLM and acceptance scenarios 4–9, 12, and 24 pass.

### Phase 3 — Disputes and verification

- Implement rebuttals, counterevidence, verification jobs, dispositions, permissions, and history API/CLI.
- Ensure state transitions are transactional and every transition is audited.
- Exit criteria: acceptance scenarios 10–11 and 22–23 pass.

### Phase 4 — Security and operational hardening

- Complete threat model and security tests.
- Add authentication, capability enforcement, rate limits, safe logging, retention, readiness checks, and dependency scanning.
- Exit criteria: acceptance scenarios 13–20 pass.

### Phase 5 — Integrations

- Add Git/CI integration first, then tracing/evaluation/policy/provenance adapters according to actual ecosystem needs.
- Each adapter must have a contract test and document permissions and failure modes.
- Exit criteria: adapters can be disabled independently and core NYX still works.

### Phase 6 — Optional model critics and calibration

- Add a pluggable LLM critic behind the same interface.
- Build an evaluation corpus from synthetic and consented examples.
- Compare against deterministic baseline; track false positives, false negatives, unsupported assertions, latency, and cost.
- Exit criteria: schema validation, evidence-ID validation, prompt-injection tests, provider-failure tests, and documented evaluation results pass.

## 20. Coding-agent task order

The coding agent should execute this sequence, updating the task checklist as it goes:

1. Repository reconnaissance and current-state report.
2. Architecture/threat model and ADRs.
3. Schemas and domain invariants.
4. Storage/migrations/repositories.
5. Event ingestion and system registry.
6. Evidence registry, redaction, hashing, and access controls.
7. Critic interface and critic registry.
8. C-001 completion-vs-verification critic.
9. Finding composition, query, and lifecycle state machine.
10. Remaining deterministic critics.
11. Rebuttal, verification, and disposition workflows.
12. Audit verification and self-consistency checks.
13. CLI and API contract completion.
14. Security hardening and negative tests.
15. Git/CI adapter.
16. Optional model critics and other adapters.
17. End-to-end demo and operational documentation.

Do not begin with an LLM critic or dashboard. The first demonstrable vertical slice should be: **ingest a scoped completion claim → attach a test result tied to a commit → run a deterministic critic → create an evidence-linked finding when the commit differs → submit a rebuttal → verify → record the disposition and full history.**

## 21. Demonstration scenario

Include a repeatable demo using synthetic data:

- System `ghost-factory-demo` registers repository commit `abc123`.
- It submits claim: “All required tests pass for commit `abc123`.”
- Evidence item E1 contains a test result for commit `def456`.
- NYX runs C-001 and creates a finding explaining that E1 does not verify the stated commit.
- The finding shows both the claim and E1, with scope and limitation.
- The system submits a rebuttal with evidence E2, a test report for `abc123`.
- NYX reruns verification and records a new disposition.
- The original finding, rebuttal, E1, E2, critic runs, and disposition history remain inspectable.

The demo must also show a negative case where the same-commit test evidence prevents a false positive.

## 22. Delivery requirements

At the end of implementation, the coding agent must produce:

1. A short architecture summary and a module map.
2. A checklist mapping each requirement to code and tests.
3. The exact setup and run commands.
4. The exact test/lint/type-check/security commands executed and their real outcomes.
5. A working synthetic end-to-end demonstration.
6. An integration status table: implemented, partial, planned, or blocked.
7. Known limitations, security assumptions, and unresolved design decisions.
8. A list of changed files and a commit/PR reference if the environment supports Git operations.
9. A clear statement of any acceptance criteria not met.

**Do not report completion based only on generated files or a successful compile.** Completion requires tested end-to-end behaviour and honest disclosure of remaining gaps.

## 23. Requirement traceability checklist

Use this checklist in the implementation PR:

- [ ] Repository reconnaissance completed; existing architecture respected.
- [ ] Schemas versioned and validated in CI.
- [ ] Systems, events, claims, and evidence can be registered and queried.
- [ ] Redaction, access control, idempotency, and payload limits are tested.
- [ ] Critic interface records versions and exact inputs.
- [ ] Deterministic critic pack implemented and individually tested.
- [ ] Findings distinguish observation, inference, recommendation, and unknowns.
- [ ] Finding lifecycle is a validated state machine.
- [ ] Rebuttals and counterevidence preserve history.
- [ ] Dispositions require permission and rationale.
- [ ] Audit history is queryable and verifiable within its documented threat model.
- [ ] API and CLI semantics are consistent.
- [ ] Security and privacy tests pass.
- [ ] No-LLM mode works.
- [ ] Synthetic end-to-end demo passes.
- [ ] External adapters are accurately labelled by implementation status.
- [ ] Documentation, setup instructions, and actual test results are included.

---

## Final instruction to the coding agent

Treat Part I as the OSS research and architectural rationale, and Part II as the implementation contract. Inspect the target repository first, then implement in the prescribed vertical slices. Resolve genuine repository-specific uncertainties by documenting an ADR; do not invent integrations, skip tests, or replace evidence with model confidence. NYX must remain criticisable: every material finding must be inspectable, contestable, versioned, and supported by evidence proportionate to its impact.
