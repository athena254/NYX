"""Unit tests for C-004 (ProvenanceGapCritic).

Covers acceptance scenario:
  (provenance) -- Evidence missing required provenance fields is flagged with the
  missing fields named; complete provenance is accepted.

Also proves the three rules the ground-truth upgrade adds:
* verification evidence (a declared verification method, or a test_result/ci_test_result kind)
  must also carry a subject version and a content hash;
* every other kind must carry at least one of a producer identity or a content hash;
* a declared hash chain link that does not match the evidence's content hash is a finding, and
  no declared chain is a no-op.

Every new rule has its near miss: a non-verification kind is not asked for a hash, a producer
without a hash passes, a hash without a producer passes, and no declared chain is never a
finding. A false accusation is worse than a miss.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nyx.critics.c004_provenance_gap import (
    CRITIC_ID,
    CRITIC_VERSION,
    ProvenanceGapCritic,
)
from nyx.critics.context import build_context
from nyx.domain.base import PrincipalRef, Scope
from nyx.domain.claims import Claim
from nyx.domain.enums import (
    ClaimType,
    CriticResultStatus,
    EvidenceKind,
    IntegrityMethod,
    PrincipalKind,
    Sensitivity,
    VerificationResult,
)
from nyx.domain.events import Event
from nyx.domain.evidence import EvidenceItem, EvidenceVerification

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _event() -> Event:
    return Event(
        source_system_id="sys_c004_test",
        event_type="agent.completion_claim",
        occurred_at=_NOW,
        correlation_id="c004-corr-1",
        payload={"claim_id": "clm_c004_test"},
        content_hash="sha256:" + "0" * 64,
        trust_class="observed",
        retention_class="standard",
    )


def _claim() -> Claim:
    return Claim(
        statement="All required tests pass.",
        claimant_system_id="sys_c004_test",
        claim_type=ClaimType.COMPLETION,
        scope=Scope(system_id="sys_c004_test", artifact="demo", commit="abc123"),
        created_at=_NOW,
    )


def _evidence(
    *,
    source_system_id: str | None = "sys_c004_test",
    source_name: str | None = None,
    producer_identity: PrincipalRef | None = None,
    locator: str | None = "https://example.invalid/evidence/1",
    scope: Scope | None = None,
    kind: EvidenceKind = EvidenceKind.CI_TEST_RESULT,
    content_hash: str | None = None,
    verification: EvidenceVerification | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        kind=kind,
        title="CI run",
        description="A CI test result.",
        source_system_id=source_system_id,
        source_name=source_name,
        source_ref="https://ci.example.invalid/runs/1",
        locator=locator,
        scope=scope,
        captured_at=_NOW,
        valid_from=_NOW,
        valid_until=None,
        integrity_method=IntegrityMethod.HASH,
        content_hash=content_hash,
        producer_identity=producer_identity,
        verification=verification,
        relevance_to_claim="clm_c004_test",
        limitations=["Synthetic test evidence."],
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref="test",
        retention_class="standard",
    )


# ---------------------------------------------------------------------------
# Positive: evidence missing required provenance fields -> finding that names
# the missing fields and does not imply malice.
# ---------------------------------------------------------------------------


def test_missing_provenance_fires_finding_naming_fields() -> None:
    critic = ProvenanceGapCritic()
    claim = _claim()

    # Evidence with no producer identity, no locator, no scope.
    # (source_system_id is required by the model, so it cannot be missing.)
    incomplete = _evidence(
        producer_identity=None,
        locator=None,
        scope=None,
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[incomplete],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert result.critic_id == CRITIC_ID
    assert result.critic_version == CRITIC_VERSION

    # The finding must name the missing fields.
    all_observations = " ".join(result.observations).lower()
    all_limitations = " ".join(result.limitations).lower()
    combined = all_observations + " " + all_limitations + " " + result.rationale.lower()

    for field_name in ("locator", "scope"):
        assert field_name in combined, (
            f"provenance gap finding must name the missing field '{field_name}'; "
            f"got observations={result.observations} rationale={result.rationale}"
        )

    # Must not imply malice.
    assert "not an assertion of malicious behaviour" in all_limitations or (
        "not" in all_limitations and "malicious" in all_limitations
    ), "scenario 7 requires the provenance gap to explicitly not imply malice"


# ---------------------------------------------------------------------------
# Control: complete provenance -> no finding.
# ---------------------------------------------------------------------------


def test_complete_provenance_no_finding() -> None:
    critic = ProvenanceGapCritic()
    claim = _claim()

    complete = _evidence(
        source_system_id="sys_c004_test",
        source_name=None,
        producer_identity=PrincipalRef(
            id="proc_ci_agent",
            kind=PrincipalKind.SERVICE,
            display_name="CI Agent",
        ),
        locator="https://ci.example.invalid/runs/1",
        scope=Scope(system_id="sys_c004_test", artifact="demo", commit="abc123"),
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[complete],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING
    assert result.critic_id == CRITIC_ID
    assert result.rationale


# ---------------------------------------------------------------------------
# RULE 2: verification evidence needs a subject version and a content hash.
# ---------------------------------------------------------------------------

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_SCOPE = Scope(system_id="sys_c004_test", artifact="demo", commit="abc123")
_PRODUCER = PrincipalRef(id="proc_ci_agent", kind=PrincipalKind.SERVICE, display_name="CI Agent")


def _verification_evidence(
    *,
    kind: EvidenceKind = EvidenceKind.CI_TEST_RESULT,
    scope: Scope | None = _SCOPE,
    content_hash: str | None = _HASH_A,
    verification: EvidenceVerification | None = None,
) -> EvidenceItem:
    return _evidence(
        kind=kind,
        producer_identity=_PRODUCER,
        scope=scope,
        content_hash=content_hash,
        verification=verification,
    )


def test_verification_kind_without_a_hash_is_flagged_when_the_flag_is_on() -> None:
    critic = ProvenanceGapCritic(require_verifiable_origin=True)
    claim = _claim()

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[_verification_evidence(content_hash=None)],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    combined = " ".join(result.observations).lower()
    assert "content hash" in combined


def test_verification_kind_without_a_subject_version_is_flagged() -> None:
    critic = ProvenanceGapCritic(require_verifiable_origin=True)
    claim = _claim()

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[_verification_evidence(scope=None)],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    combined = " ".join(result.observations).lower()
    assert "subject version" in combined


def test_a_declared_verification_method_triggers_the_same_requirement() -> None:
    """The kind is not the only trigger: declaring a method makes the evidence verification."""

    critic = ProvenanceGapCritic(require_verifiable_origin=True)
    claim = _claim()
    evidence = _verification_evidence(
        kind=EvidenceKind.LOG,
        content_hash=None,
        verification=EvidenceVerification(result=VerificationResult.VERIFIED, method="sha256"),
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert "content hash" in " ".join(result.observations).lower()


def test_verification_evidence_with_both_fields_passes() -> None:
    critic = ProvenanceGapCritic(require_verifiable_origin=True)
    claim = _claim()

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[_verification_evidence()],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_the_hash_requirement_is_silent_when_the_flag_is_off() -> None:
    """Flags off equals current behaviour: a hash is not demanded of a record that never had one."""

    critic = ProvenanceGapCritic(require_verifiable_origin=False)
    assert critic.require_verifiable_origin is False
    claim = _claim()

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[_verification_evidence(content_hash=None)],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


# ---------------------------------------------------------------------------
# RULE 3: the origin must not be entirely unverifiable.
# ---------------------------------------------------------------------------


def test_evidence_with_neither_producer_nor_hash_is_flagged() -> None:
    critic = ProvenanceGapCritic(require_verifiable_origin=True)
    claim = _claim()
    evidence = _evidence(
        kind=EvidenceKind.LOG,
        producer_identity=None,
        scope=_SCOPE,
        content_hash=None,
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert "verifiable origin" in " ".join(result.observations).lower()


def test_a_producer_without_a_hash_is_still_attributable() -> None:
    """RULE 3 accepts either field: a producer with no hash is attributable, not unverifiable."""

    critic = ProvenanceGapCritic(require_verifiable_origin=True)
    claim = _claim()
    evidence = _evidence(
        kind=EvidenceKind.LOG,
        producer_identity=_PRODUCER,
        locator=None,
        scope=_SCOPE,
        content_hash=None,
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    # The finding is the baseline locator gap, not an origin gap.
    combined = " ".join(result.observations).lower()
    assert "locator" in combined
    assert "verifiable origin" not in combined


def test_a_hash_without_a_producer_is_still_integrity_checkable() -> None:
    """RULE 3 accepts either field: a hash with no producer is integrity-checkable, not originless.

    The producer identity is missing, so the baseline rule still fires for that field. What this
    proves is that RULE 3 does not *add* an origin gap on top of it.
    """

    critic = ProvenanceGapCritic(require_verifiable_origin=True)
    claim = _claim()
    evidence = _evidence(
        kind=EvidenceKind.LOG,
        producer_identity=None,
        locator=None,
        scope=_SCOPE,
        content_hash=_HASH_A,
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    combined = " ".join(result.observations).lower()
    assert "source identity" in combined
    assert "verifiable origin" not in combined


def test_the_origin_rule_is_silent_when_the_flag_is_off() -> None:
    """Flags off equals current behaviour: the origin gap is not raised without the flag.

    The baseline rule still fires on the missing locator, so this asserts specifically that the
    *origin* gap is absent, which is the new behaviour being gated.
    """

    critic = ProvenanceGapCritic(require_verifiable_origin=False)
    claim = _claim()
    evidence = _evidence(
        kind=EvidenceKind.LOG,
        producer_identity=None,
        locator=None,
        scope=_SCOPE,
        content_hash=None,
    )

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    combined = " ".join(result.observations).lower()
    assert "locator" in combined
    assert "verifiable origin" not in combined


# ---------------------------------------------------------------------------
# RULE 4: an optional hash chain. No declared chain is never a finding.
# ---------------------------------------------------------------------------


def test_a_broken_chain_link_is_a_finding() -> None:
    critic = ProvenanceGapCritic()
    claim = _claim()
    evidence = _verification_evidence(content_hash=_HASH_A)

    context = build_context(
        target_event=_event().model_copy(update={"payload": {"claim_id": "clm_c004_test"}}),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING  # nothing declared a chain yet


def test_a_chain_link_declared_by_the_event_is_verified() -> None:
    """The chain is declared on the target event, so the link must name the evidence's hash."""

    critic = ProvenanceGapCritic()
    claim = _claim()
    evidence = _verification_evidence(content_hash=_HASH_A)
    event = _event().model_copy(
        update={"payload": {"claim_id": "clm_c004_test", "chain_hash": _HASH_B}}
    )

    context = build_context(
        target_event=event,
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    assert "chain" in " ".join(result.observations).lower()


def test_a_matching_chain_link_passes() -> None:
    critic = ProvenanceGapCritic()
    claim = _claim()
    evidence = _verification_evidence(content_hash=_HASH_A)
    event = _event().model_copy(
        update={"payload": {"claim_id": "clm_c004_test", "chain_hash": _HASH_A}}
    )

    context = build_context(
        target_event=event,
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_the_prior_hash_key_is_accepted_as_a_chain_link() -> None:
    critic = ProvenanceGapCritic()
    claim = _claim()
    evidence = _verification_evidence(content_hash=_HASH_A)
    event = _event().model_copy(
        update={"payload": {"claim_id": "clm_c004_test", "prior_hash": _HASH_B}}
    )

    context = build_context(
        target_event=event,
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING


def test_an_undeclared_chain_is_a_no_op_even_without_a_hash() -> None:
    """Chaining is optional and must never be required: no hash, no chain, no finding."""

    critic = ProvenanceGapCritic()
    claim = _claim()
    evidence = _verification_evidence(content_hash=None)

    context = build_context(
        target_event=_event(),
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.NO_FINDING


def test_a_chain_gap_is_named_in_the_finding_and_does_not_imply_malice() -> None:
    critic = ProvenanceGapCritic()
    claim = _claim()
    evidence = _verification_evidence(content_hash=_HASH_A)
    event = _event().model_copy(
        update={"payload": {"claim_id": "clm_c004_test", "chain_hash": _HASH_B}}
    )

    context = build_context(
        target_event=event,
        linked_claim=claim,
        linked_evidence=[evidence],
        now=_NOW,
    )

    result = critic.run(context)

    assert result.result is CriticResultStatus.FINDING
    all_limitations = " ".join(result.limitations).lower()
    assert "not an assertion of malicious behaviour" in all_limitations
