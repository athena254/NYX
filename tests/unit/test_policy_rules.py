"""Unit tests for the declarative policy rules (SPEC §8, C-005).

Proves that ``PolicyRule`` is usable data rather than decoration:

* each of the five kinds fires on a matching payload and passes on a near miss;
* a rule that applies to no key in the payload is ``NOT_APPLICABLE``, never ``VIOLATED``;
* a rule that is not ``enabled`` cannot decide anything;
* an unrecognised data-handling limit is not a violation;
* ``to_dict`` is the serialisable record form a policy version can store;
* C-005 evaluates declarative rules from the registry alongside the callables, and an action
  governed by no known policy is ``no_finding`` — the policy model has no such concept.

Every kind has its near miss, because a false accusation is worse than a miss.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.critics.c005_policy_violation import CRITIC_ID, PolicyViolationCritic
from nyx.critics.context import build_context
from nyx.domain.base import PrincipalRef, Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimType,
    CriticResultStatus,
    EvidenceKind,
    FindingCategory,
    IntegrityMethod,
    PrincipalKind,
    Sensitivity,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem
from nyx.policies.engine import PolicyDecision
from nyx.policies.rules import PolicyRule, PolicyRuleKind

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _rule(rule_id: str, kind: PolicyRuleKind, **kwargs) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        policy_version="1.0.0",
        kind=kind,
        description=f"Declarative test rule {rule_id}.",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# FORBIDDEN_ACTION
# ---------------------------------------------------------------------------


def test_forbidden_action_fires_on_a_match() -> None:
    rule = _rule(
        "action.no-drop-table",
        PolicyRuleKind.FORBIDDEN_ACTION,
        forbidden_actions=["drop_table", "truncate"],
    )

    evaluation = rule.evaluate({"actions": ["select", "drop_table"]})

    assert evaluation.decision is PolicyDecision.VIOLATED
    assert evaluation.rule_version == "1.0.0"
    assert "drop_table" in evaluation.rationale


def test_forbidden_action_passes_on_a_near_miss() -> None:
    """An action outside the declared set is not a violation: the vocabulary is closed."""

    rule = _rule(
        "action.no-drop-table",
        PolicyRuleKind.FORBIDDEN_ACTION,
        forbidden_actions=["drop_table"],
    )

    assert rule.evaluate({"actions": ["select", "insert"]}).decision is PolicyDecision.SATISFIED
    assert rule.evaluate({"actions": ["drop_table_column"]}).decision is PolicyDecision.SATISFIED


def test_forbidden_action_is_not_applicable_without_the_key() -> None:
    rule = _rule(
        "action.no-drop-table",
        PolicyRuleKind.FORBIDDEN_ACTION,
        forbidden_actions=["drop_table"],
    )

    assert rule.evaluate({"claim_type": "completion"}).decision is PolicyDecision.NOT_APPLICABLE
    assert rule.matches({"claim_type": "completion"}) is False


# ---------------------------------------------------------------------------
# REQUIRED_STEP
# ---------------------------------------------------------------------------


def test_required_step_fires_on_a_missing_step() -> None:
    rule = _rule(
        "process.migration-reviewed",
        PolicyRuleKind.REQUIRED_STEP,
        required_steps=["migration_reviewed", "backup_verified"],
    )

    evaluation = rule.evaluate({"completed_steps": ["backup_verified"]})

    assert evaluation.decision is PolicyDecision.VIOLATED
    assert "migration_reviewed" in evaluation.rationale


def test_required_step_passes_when_all_steps_are_recorded() -> None:
    rule = _rule(
        "process.migration-reviewed",
        PolicyRuleKind.REQUIRED_STEP,
        required_steps=["migration_reviewed"],
    )

    assert (
        rule.evaluate({"completed_steps": ["migration_reviewed", "backup_verified"]}).decision
        is PolicyDecision.SATISFIED
    )


def test_required_step_passes_when_the_payload_says_nothing_about_steps() -> None:
    """A payload with no step list is unknown, not non-compliant."""

    rule = _rule(
        "process.migration-reviewed",
        PolicyRuleKind.REQUIRED_STEP,
        required_steps=["migration_reviewed"],
    )

    assert rule.evaluate({"actions": ["deploy"]}).decision is PolicyDecision.NOT_APPLICABLE


# ---------------------------------------------------------------------------
# REQUIRED_APPROVER
# ---------------------------------------------------------------------------


def test_required_approver_fires_when_no_required_role_is_present() -> None:
    rule = _rule(
        "approval.two-person-rule",
        PolicyRuleKind.REQUIRED_APPROVER,
        required_approver_roles=["security_lead", "data_owner"],
    )

    evaluation = rule.evaluate({"approver_roles": ["engineer"]})

    assert evaluation.decision is PolicyDecision.VIOLATED


def test_required_approver_passes_when_one_required_role_is_present() -> None:
    rule = _rule(
        "approval.two-person-rule",
        PolicyRuleKind.REQUIRED_APPROVER,
        required_approver_roles=["security_lead", "data_owner"],
    )

    assert (
        rule.evaluate({"approver_roles": ["engineer", "data_owner"]}).decision
        is PolicyDecision.SATISFIED
    )


def test_required_approver_passes_when_the_payload_declares_no_roles() -> None:
    rule = _rule(
        "approval.two-person-rule",
        PolicyRuleKind.REQUIRED_APPROVER,
        required_approver_roles=["security_lead"],
    )

    assert rule.evaluate({"actions": ["deploy"]}).decision is PolicyDecision.NOT_APPLICABLE


# ---------------------------------------------------------------------------
# ALLOWED_ENVIRONMENT
# ---------------------------------------------------------------------------


def test_allowed_environment_fires_on_a_disallowed_environment() -> None:
    rule = _rule(
        "deploy.no-production-from-ci",
        PolicyRuleKind.ALLOWED_ENVIRONMENT,
        allowed_environments=["staging", "development"],
    )

    evaluation = rule.evaluate({"environment": "production"})

    assert evaluation.decision is PolicyDecision.VIOLATED
    assert "production" in evaluation.rationale


def test_allowed_environment_passes_on_an_allowed_environment() -> None:
    rule = _rule(
        "deploy.no-production-from-ci",
        PolicyRuleKind.ALLOWED_ENVIRONMENT,
        allowed_environments=["staging", "development"],
    )

    assert rule.evaluate({"environment": "staging"}).decision is PolicyDecision.SATISFIED


def test_allowed_environment_is_not_applicable_without_the_key() -> None:
    rule = _rule(
        "deploy.no-production-from-ci",
        PolicyRuleKind.ALLOWED_ENVIRONMENT,
        allowed_environments=["staging"],
    )

    assert rule.evaluate({}).decision is PolicyDecision.NOT_APPLICABLE


# ---------------------------------------------------------------------------
# DATA_HANDLING
# ---------------------------------------------------------------------------


def test_data_handling_fires_when_a_limit_is_exceeded() -> None:
    rule = _rule(
        "data.no-bulk-export",
        PolicyRuleKind.DATA_HANDLING,
        data_handling_limits={"max_rows": "1000"},
    )

    evaluation = rule.evaluate({"data_handling": {"max_rows": 5000}})

    assert evaluation.decision is PolicyDecision.VIOLATED
    assert "max_rows" in evaluation.rationale


def test_data_handling_passes_at_the_limit() -> None:
    """The limit is inclusive: exactly at it is not a breach."""

    rule = _rule(
        "data.no-bulk-export",
        PolicyRuleKind.DATA_HANDLING,
        data_handling_limits={"max_rows": "1000"},
    )

    assert rule.evaluate({"data_handling": {"max_rows": 1000}}).decision is PolicyDecision.SATISFIED


def test_data_handling_ignores_an_attribute_it_does_not_name() -> None:
    rule = _rule(
        "data.no-bulk-export",
        PolicyRuleKind.DATA_HANDLING,
        data_handling_limits={"max_rows": "1000"},
    )

    assert (
        rule.evaluate({"data_handling": {"max_columns": 99999}}).decision
        is PolicyDecision.SATISFIED
    )


def test_an_unparseable_limit_is_not_a_violation() -> None:
    """An unrecognised token is not a violation: guessing a unit would manufacture findings."""

    rule = _rule(
        "data.no-bulk-export",
        PolicyRuleKind.DATA_HANDLING,
        data_handling_limits={"max_rows": "one-thousand-rows"},
    )

    assert rule.evaluate({"data_handling": {"max_rows": 5000}}).decision is PolicyDecision.SATISFIED


def test_data_handling_is_not_applicable_without_the_key() -> None:
    rule = _rule(
        "data.no-bulk-export",
        PolicyRuleKind.DATA_HANDLING,
        data_handling_limits={"max_rows": "1000"},
    )

    assert rule.evaluate({}).decision is PolicyDecision.NOT_APPLICABLE


# ---------------------------------------------------------------------------
# The rule as data: disabled rules, serialisation, and the payload contract.
# ---------------------------------------------------------------------------


def test_a_disabled_rule_cannot_decide_anything() -> None:
    rule = _rule(
        "action.no-drop-table",
        PolicyRuleKind.FORBIDDEN_ACTION,
        forbidden_actions=["drop_table"],
        enabled=False,
    )

    assert rule.matches({"actions": ["drop_table"]}) is False
    assert rule.evaluate({"actions": ["drop_table"]}).decision is PolicyDecision.NOT_APPLICABLE


def test_a_rule_with_no_constraints_does_not_match() -> None:
    """An empty rule set is not a constraint: it must not read as satisfied-by-default."""

    rule = _rule("action.empty", PolicyRuleKind.FORBIDDEN_ACTION)

    assert rule.matches({"actions": ["drop_table"]}) is False
    assert rule.evaluate({"actions": ["drop_table"]}).decision is PolicyDecision.NOT_APPLICABLE


def test_to_dict_is_the_serialisable_record_form() -> None:
    rule = _rule(
        "action.no-drop-table",
        PolicyRuleKind.FORBIDDEN_ACTION,
        forbidden_actions=["drop_table"],
    )

    record = rule.to_dict()

    assert record["kind"] == "forbidden_action"
    assert record["forbidden_actions"] == ["drop_table"]
    assert record["policy_version"] == "1.0.0"
    # The rule id is the registry key, not part of the rule's own content.
    assert "rule_id" not in record


def test_a_single_string_is_read_as_a_one_element_list() -> None:
    """Producer payloads are untrusted: a bare string is tolerated rather than silently dropped."""

    rule = _rule(
        "action.no-drop-table",
        PolicyRuleKind.FORBIDDEN_ACTION,
        forbidden_actions=["drop_table"],
    )

    assert rule.evaluate({"actions": "drop_table"}).decision is PolicyDecision.VIOLATED


def test_a_non_string_action_is_ignored() -> None:
    rule = _rule(
        "action.no-drop-table",
        PolicyRuleKind.FORBIDDEN_ACTION,
        forbidden_actions=["drop_table"],
    )

    assert rule.evaluate({"actions": [1, None, "select"]}).decision is PolicyDecision.SATISFIED


# ---------------------------------------------------------------------------
# C-005 wiring: declarative rules are evaluated beside the callables.
# ---------------------------------------------------------------------------


def _event(**payload) -> Event:
    base = {
        "claim_id": "clm_c005_decl",
        "actions": [],
        "completed_steps": [],
        "approver_roles": [],
        "environment": None,
        "data_handling": {},
    }
    base.update(payload)
    return Event(
        source_system_id="sys_c005_decl",
        event_type="agent.completion_claim",
        occurred_at=_NOW,
        correlation_id="c005-decl-corr-1",
        payload=base,
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )


def _claim() -> Claim:
    return Claim(
        statement="The deployment completed successfully.",
        claimant_system_id="sys_c005_decl",
        claimant_principal_id="user_alice",
        claim_type=ClaimType.COMPLETION,
        scope=Scope(system_id="sys_c005_decl", artifact="demo", commit="abc123"),
        created_at=_NOW,
    )


def _review(producer: str = "user_bob") -> EvidenceItem:
    return EvidenceItem(
        kind=EvidenceKind.REVIEW_RECORD,
        title="Approval record",
        description="An approval record for the deployment.",
        source_system_id="sys_c005_decl",
        source_ref="https://example.invalid/review/1",
        locator="https://example.invalid/review/1",
        scope=Scope(system_id="sys_c005_decl", artifact="demo", commit="abc123"),
        captured_at=_NOW,
        valid_from=_NOW,
        valid_until=None,
        integrity_method=IntegrityMethod.HASH,
        producer_identity=PrincipalRef(
            id=producer, kind=PrincipalKind.HUMAN, display_name=producer
        ),
        relevance_to_claim="clm_c005_decl",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )


def _verification(producer: str = "svc_ci") -> EvidenceItem:
    """A passing verification record, so the callable completion rule is satisfied."""

    return EvidenceItem(
        kind=EvidenceKind.CI_TEST_RESULT,
        title="CI run passed",
        description="All required checks passed.",
        source_system_id="sys_c005_decl",
        source_ref="https://example.invalid/ci/1",
        locator="https://example.invalid/ci/1",
        scope=Scope(system_id="sys_c005_decl", artifact="demo", commit="abc123"),
        captured_at=_NOW,
        valid_from=_NOW,
        valid_until=None,
        integrity_method=IntegrityMethod.HASH,
        producer_identity=PrincipalRef(
            id=producer, kind=PrincipalKind.SERVICE, display_name=producer
        ),
        relevance_to_claim="clm_c005_decl",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )


def _declarative_context(*, event: Event) -> object:
    """A context whose callable rules are all satisfied, so only declarative rules can fire.

    Verification evidence and an approval record from a principal other than the claimant keep
    all three callable rules satisfied; otherwise a passing declarative rule would look like a
    finding raised by the callables.
    """

    return build_context(
        target_event=event,
        linked_claim=_claim(),
        linked_evidence=[_verification(), _review()],
        now=_NOW,
    )


def test_a_declarative_rule_fires_through_the_critic() -> None:
    rule = _rule(
        "deploy.no-production-from-ci",
        PolicyRuleKind.ALLOWED_ENVIRONMENT,
        allowed_environments=["staging"],
    )
    critic = PolicyViolationCritic(declarative_rules=[rule])

    result = critic.run(_declarative_context(event=_event(environment="production")))

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    assert result.category is FindingCategory.POLICY_VIOLATION
    combined = " ".join(result.observations) + " " + result.rationale
    assert "deploy.no-production-from-ci" in combined
    assert "declarative" in combined


def test_a_declarative_rule_passes_on_a_near_miss_through_the_critic() -> None:
    rule = _rule(
        "deploy.no-production-from-ci",
        PolicyRuleKind.ALLOWED_ENVIRONMENT,
        allowed_environments=["staging"],
    )
    critic = PolicyViolationCritic(declarative_rules=[rule])

    result = critic.run(_declarative_context(event=_event(environment="staging")))

    assert result.result is CriticResultStatus.NO_FINDING


def test_an_action_governed_by_no_known_policy_is_not_a_finding() -> None:
    """The policy model has no 'ungoverned action' concept, so this must stay quiet.

    Documented in the critic's ``cannot_assess`` and its module docstring rather than papered
    over with an invented violation.
    """

    rule = _rule(
        "deploy.no-production-from-ci",
        PolicyRuleKind.ALLOWED_ENVIRONMENT,
        allowed_environments=["staging"],
    )
    critic = PolicyViolationCritic(declarative_rules=[rule])

    result = critic.run(_declarative_context(event=_event(environment=None)))

    assert result.result is CriticResultStatus.NO_FINDING
    assert any(
        "no known policy" in item or "no such concept" in item
        for item in critic.scope.cannot_assess
    )


def test_the_callable_registry_still_decides_when_no_declarative_rule_is_registered() -> None:
    """Default behaviour is unchanged: the three callable rules are the whole rule set.

    With no declarative rule registered the callable rules decide alone, and the payload they
    see is exactly the payload they saw before the declarative half of the model existed — the
    extra keys are added only when a declarative rule is present.
    """

    critic = PolicyViolationCritic()
    event = _event()
    context = build_context(
        target_event=event,
        linked_claim=_claim(),
        linked_evidence=[_review()],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    combined = " ".join(result.observations) + " " + result.rationale
    assert "completion.evidence-required" in combined
    # The payload the callables saw is unchanged: no declarative key leaked in.
    assert '"actions"' not in combined


def test_a_declarative_rule_is_additive_to_the_callable_findings() -> None:
    rule = _rule(
        "action.no-drop-table",
        PolicyRuleKind.FORBIDDEN_ACTION,
        forbidden_actions=["drop_table"],
    )
    critic = PolicyViolationCritic(declarative_rules=[rule])

    result = critic.run(_declarative_context(event=_event(actions=["drop_table"])))

    assert result.result is CriticResultStatus.FINDING
    combined = " ".join(result.observations)
    # The declarative rule fires and the callable rule set stays satisfied, so the only
    # violation named is the declarative one.
    assert "action.no-drop-table" in combined
    assert "completion.evidence-required" not in combined
    assert "approval.required-recorded" not in combined


def test_the_no_finding_rationale_records_the_ungoverned_action_limit() -> None:
    rule = _rule(
        "action.no-drop-table",
        PolicyRuleKind.FORBIDDEN_ACTION,
        forbidden_actions=["drop_table"],
    )
    critic = PolicyViolationCritic(declarative_rules=[rule])

    result = critic.run(_declarative_context(event=_event(actions=["select"])))

    assert result.result is CriticResultStatus.NO_FINDING
    assert "no known policy" in result.rationale
