"""Unit tests for C-012 (ScopeCreepCritic).

Covers SPEC §8 C-012: a recorded action that reaches outside the task's declared scope, or a
permission-granting action the task did not request.

The control tests matter most here. This critic must stay quiet when every recorded action is
inside the declared allow-list, when the verb is unrecognised, when no evidence and no action are
recorded, and when the task declared no scope at all -- otherwise it would accuse every routine
operation of scope creep.
"""

from __future__ import annotations

from typing import Any

from nyx.critics.c012_scope_creep import (
    CRITIC_ID,
    CRITIC_VERSION,
    ScopeCreepCritic,
)
from nyx.critics.context import CriticContext, build_context
from nyx.domain.base import Scope
from nyx.domain.canonical import content_hash
from nyx.domain.claims import Claim
from nyx.domain.enums import ClaimType, CriticResultStatus, EvidenceKind, FindingCategory, Severity
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem

_SYSTEM = "sys_agent"
_NOW = "2026-10-19T10:00:00Z"


def _event(payload: dict[str, Any]) -> Event:
    return Event(
        source_system_id=_SYSTEM,
        event_type="agent.action_recorded",
        occurred_at=_NOW,
        correlation_id="c012-corr",
        payload=payload,
        content_hash=content_hash(payload),
        trust_class="observed",
        retention_class="standard",
    )


def _scope(*, commit: str | None = "abc123") -> Scope:
    return Scope(system_id=_SYSTEM, artifact="demo-service", commit=commit, environment="staging")


def _claim(scope: Scope | None = None) -> Claim:
    return Claim(
        statement="The migration ran inside the declared paths.",
        claimant_system_id=_SYSTEM,
        claim_type=ClaimType.OTHER,
        scope=scope if scope is not None else _scope(),
    )


def _evidence(
    *,
    evidence_id: str = "ev_scope",
    locator: str | None = None,
    source_ref: str = "https://logs.example.invalid/scope",
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        kind=EvidenceKind.LOG,
        title="Tool call log",
        description="The agent's recorded tool calls for this task.",
        source_ref=source_ref,
        locator=locator,
        source_system_id=_SYSTEM,
        relevance_to_finding="fnd_placeholder",
        captured_at=_NOW,
        valid_from=_NOW,
        integrity_method="none",
        limitations=["Synthetic test evidence."],
        sensitivity="internal",
        access_policy_ref="test",
        retention_class="standard",
    )


def _context(
    payload: dict[str, Any],
    *,
    evidence: list[EvidenceItem] | None = None,
    claim: Claim | None = None,
    scope: Scope | None = None,
) -> CriticContext:
    """Build the context directly, preferring an explicit scope when one is supplied."""

    context = build_context(
        target_event=_event(payload),
        linked_claim=claim,
        related_claims=[],
        linked_evidence=evidence or [],
        now=None,
    )
    if scope is None:
        # A claim's scope wins when one is linked; otherwise the test supplies a default scope so
        # the critic has a subject version to report.
        scope = context.linked_claim.scope if context.linked_claim is not None else _scope()
    return CriticContext(
        target_event=context.target_event,
        linked_claim=context.linked_claim,
        related_claims=context.related_claims,
        linked_evidence=context.linked_evidence,
        observations=context.observations,
        scope=scope,
        now=context.now,
    )


def _run(
    payload: dict[str, Any],
    *,
    evidence: list[EvidenceItem] | None = None,
    claim: Claim | None = None,
    scope: Scope | None = None,
):
    return ScopeCreepCritic().run(_context(payload, evidence=evidence, claim=claim, scope=scope))


# --------------------------------------------------------------------------- true positives


def test_out_of_scope_path_is_flagged() -> None:
    result = _run(
        {
            "task_scope": {"paths": ["/srv/app/migrations"]},
            "action": "write",
            "path": "/srv/app/secrets/credentials.json",
        }
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION
    assert result.severity is Severity.MEDIUM
    assert result.category is FindingCategory.OTHER
    assert result.requires_human_review is False
    assert "/srv/app/secrets/credentials.json" in result.proposition + result.summary


def test_permission_grant_is_flagged_high_and_requires_review() -> None:
    result = _run(
        {
            "task_scope": {"paths": ["/srv/app"]},
            "action": "grant",
            "target": "role/admin",
        }
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.severity is Severity.HIGH
    assert result.category is FindingCategory.SECURITY_RISK
    assert result.requires_human_review is True


def test_finding_is_deterministic_and_grounded() -> None:
    """The root cause key and subject version are set and stable across identical inputs."""

    payload = {
        "task_scope": {"paths": ["/srv/app/migrations"]},
        "action": "read",
        "path": "/etc/shadow",
    }
    first = _run(payload)
    second = _run(payload)

    assert first.result is CriticResultStatus.FINDING
    assert first.root_cause_key is not None and first.root_cause_key.startswith("sha256:")
    assert first.root_cause_key == second.root_cause_key
    assert first.subject_version == "abc123"
    assert first.verification_strength is not None
    assert first.verification_strength.value == "self_reported"
    assert first.event_ids
    assert first.affected_system_ids == [_SYSTEM]


def test_out_of_scope_target_in_linked_evidence_is_flagged() -> None:
    """A path the agent touched and then recorded as evidence is still assessed."""

    result = _run(
        {
            "task_scope": {"paths": ["/srv/app/migrations"]},
            "action": "read",
            "path": "/srv/app/migrations/001.sql",
        },
        evidence=[_evidence(locator="/srv/app/secrets/id_rsa")],
    )

    assert result.result is CriticResultStatus.FINDING
    assert "/srv/app/secrets/id_rsa" in " ".join(result.observations)


def test_prose_permission_change_is_flagged() -> None:
    """A record that describes the permission change in prose is still a permission change."""

    result = _run(
        {"allowed_paths": ["/srv/app"], "action": "update_permissions", "resource": "team-lead"}
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.category is FindingCategory.SECURITY_RISK


def test_finding_does_not_assert_malicious_intent() -> None:
    result = _run(
        {"task_scope": {"paths": ["/srv/app"]}, "action": "read", "path": "/srv/other/file.txt"}
    )

    assert result.result is CriticResultStatus.FINDING
    joined = (" ".join(result.limitations) + " " + result.rationale).lower()
    assert "not proof" in joined
    assert "malicious intent" in joined
    assert "does not detect" in joined
    assert result.unknowns


# --------------------------------------------------------------------------- true negatives


def test_in_scope_action_passes() -> None:
    result = _run(
        {
            "task_scope": {"paths": ["/srv/app/migrations"]},
            "action": "read",
            "path": "/srv/app/migrations/001.sql",
        }
    )

    assert result.result is CriticResultStatus.NO_FINDING


def test_subpath_of_a_declared_path_passes() -> None:
    """A deeper path under a declared directory is inside the declared scope."""

    result = _run(
        {
            "allowed_paths": ["/srv/app/migrations"],
            "action": "write",
            "path": "/srv/app/migrations/002.down.sql",
        }
    )

    assert result.result is CriticResultStatus.NO_FINDING


def test_declared_host_admits_its_uri_paths() -> None:
    result = _run(
        {
            "allowed_paths": ["https://api.example.com/v1/orders"],
            "action": "read",
            "url": "https://api.example.com/v1/orders/42",
        }
    )

    assert result.result is CriticResultStatus.NO_FINDING


def test_unrecognised_verb_stays_quiet() -> None:
    """Near-miss: an unrecognised verb is not a violation, so it is never accused."""

    result = _run(
        {"task_scope": {"paths": ["/srv/app"]}, "action": "frobnicate", "path": "/srv/app/notes.md"}
    )

    assert result.result is CriticResultStatus.NO_FINDING


def test_no_evidence_and_no_payload_action_means_no_finding() -> None:
    """Absent telemetry is unknown, not misconduct."""

    result = _run({"task_scope": {"paths": ["/srv/app"]}})

    assert result.result is CriticResultStatus.NO_FINDING
    assert "no out-of-scope access" in result.rationale


def test_missing_task_scope_means_no_finding() -> None:
    result = _run({"action": "read", "path": "/srv/app/migrations/001.sql"})

    assert result.result is CriticResultStatus.NO_FINDING
    assert "declares no path or resource scope" in result.proposition


def test_requested_permission_passes() -> None:
    """A permission change the task asked for is not a finding."""

    result = _run(
        {
            "task_scope": {"paths": ["/srv/app"]},
            "requested_permissions": ["grant_readonly"],
            "action": "grant_readonly",
            "target": "role/readonly",
        }
    )

    assert result.result is CriticResultStatus.NO_FINDING


def test_bare_resource_word_is_not_treated_as_out_of_scope() -> None:
    """A resource with no path and no host is unparseable, so it cannot be shown to be outside."""

    result = _run(
        {"task_scope": {"paths": ["/srv/app"]}, "action": "write", "resource": "deploy"}
    )

    assert result.result is CriticResultStatus.NO_FINDING


def test_prose_mention_of_a_path_is_not_an_action_on_it() -> None:
    """A free-text field that mentions a path must not manufacture a finding."""

    result = _run(
        {"task_scope": {"paths": ["/srv/app"]}, "action": "read", "path": "/srv/app/notes.md"},
        evidence=[_evidence(source_ref="https://wiki.example.invalid/out/of/scope/policy")],
    )

    assert result.result is CriticResultStatus.NO_FINDING


def test_critic_declares_a_narrow_scope_and_deteministic_type() -> None:
    critic = ScopeCreepCritic()

    assert critic.critic_id == CRITIC_ID
    assert critic.critic_version == CRITIC_VERSION
    assert critic.critic_type.value == "deterministic"
    assert critic.scope.can_assess
    assert critic.scope.cannot_assess
    assert not any("malicious intent" in item for item in critic.scope.can_assess)


def test_finding_carries_a_stable_root_cause_key_per_scope() -> None:
    """Different scopes produce different keys; the same scope and action produce the same key."""

    payload = {"task_scope": {"paths": ["/srv/app"]}, "action": "read", "path": "/srv/other"}
    first = _run(payload, scope=_scope(commit="abc123"))
    same = _run(payload, scope=_scope(commit="abc123"))
    other = _run(payload, scope=_scope(commit="def456"))

    assert first.root_cause_key == same.root_cause_key
    assert first.root_cause_key != other.root_cause_key
    assert other.subject_version == "def456"


def test_claim_scope_can_carry_the_subject_version() -> None:
    """When a claim is linked, its scope supplies the subject version."""

    claim = _claim(_scope(commit="cafe01"))
    result = _run(
        {"task_scope": {"paths": ["/srv/app"]}, "action": "read", "path": "/srv/other/file"},
        claim=claim,
    )

    assert result.result is CriticResultStatus.FINDING
    assert result.subject_version == "cafe01"
    assert result.claim_ids == [claim.id]
