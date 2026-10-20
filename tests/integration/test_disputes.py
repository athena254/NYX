"""Acceptance scenarios 10 and 11: rebuttals append, counterevidence decides.

Scenario 10 -- a rebuttal appends to history and does not overwrite the original finding. The
assertion that matters is not "a rebuttal was stored" but "the finding is otherwise unchanged": its
title, summary, reasoning, and evidence links are byte-identical afterwards, and only its status
pointer and its accumulated counterevidence moved.

Scenario 11 -- counterevidence can lead to rejection or to an inconclusive disposition. The negative
case is the one worth having: a challenged finding cannot be dismissed as rejected or inconclusive
without recording what was weighed, because a rebuttal is an answerable challenge.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from nyx.disputes.service import DisputeService, UnknownEvidenceError
from nyx.domain.base import PrincipalRef
from nyx.domain.enums import (
    DispositionOutcome,
    FindingStatus,
    PrincipalKind,
    RebuttalRequestedOutcome,
)
from nyx.findings.lifecycle import (
    CapabilityDeniedError,
    FindingLifecycle,
    TransitionRequest,
    allowed_transitions,
)
from nyx.findings.models import Finding
from nyx.storage.db import session_scope
from nyx.storage.repositories.findings import FindingRepository

API = "/api/v1"
_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
_ACTOR = PrincipalRef(id="principal_operator", kind=PrincipalKind.HUMAN)


class _AllowAllChecker:
    """Test double for the Phase-4 seam: grants every capability."""

    def has_capability(self, principal: PrincipalRef, capability: object) -> bool:
        return True


def _seed(client: TestClient, evidence_commit: str = "def456") -> tuple[str, str, str]:
    """Register a system, claim, event and evidence; run C-001; return ids.

    Returns ``(finding_id, claim_id, evidence_id)``. With ``evidence_commit='def456'`` the evidence
    does not match the claim's commit, so C-001 raises a finding.
    """

    sys_id = client.post(
        f"{API}/systems",
        json={
            "name": "dispute-sys",
            "kind": "agent",
            "environment": "development",
            "owner": "test",
            "adapter": "http",
        },
    ).json()["id"]

    claim_id = client.post(
        f"{API}/claims",
        json={
            "statement": "All required tests pass for commit abc123.",
            "claimant_system_id": sys_id,
            "claim_type": "completion",
            "scope": {"system_id": sys_id, "artifact": "demo", "commit": "abc123"},
            "producer_event_id": "d-claim-1",
        },
    ).json()["id"]

    event_id = client.post(
        f"{API}/events",
        json={
            "source_system_id": sys_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-19T10:00:00Z",
            "correlation_id": "d-corr-1",
            "payload": {"claim_id": claim_id},
            "producer_event_id": "d-evt-1",
            "trust_class": "observed",
            "labels": ["dispute"],
            "retention_class": "standard",
        },
    ).json()["id"]

    evidence_id = client.post(
        f"{API}/evidence",
        json={
            "kind": "ci_test_result",
            "title": "Required integration suite passed",
            "description": f"CI run for commit {evidence_commit}: all checks passed.",
            "source_ref": "https://ci.example.invalid/run/1",
            "source_system_id": sys_id,
            "scope": {"system_id": sys_id, "artifact": "demo", "commit": evidence_commit},
            "captured_at": "2026-10-19T09:00:00Z",
            "valid_from": "2026-10-19T09:00:00Z",
            "integrity_method": "none",
            "relevance_to_claim": claim_id,
            "limitations": ["Synthetic test evidence."],
            "sensitivity": "internal",
            "access_policy_ref": "test",
            "retention_class": "standard",
            "producer_event_id": "d-ev-1",
        },
    ).json()["id"]

    run = client.post(
        f"{API}/critics/run",
        json={"event_id": event_id, "critic_ids": ["c001-completion-vs-verification"]},
    )
    assert run.status_code == 200, run.text
    finding_id = run.json()["results"][0]["finding_id"]
    assert finding_id, "C-001 did not produce a finding"
    return finding_id, claim_id, evidence_id


def _finding(session: Session, finding_id: str) -> Finding:
    finding = FindingRepository(session).get(finding_id)
    assert finding is not None
    return finding


def _service(session: Session) -> DisputeService:
    return DisputeService(session, capability_checker=_AllowAllChecker(), clock=lambda: _NOW)


def _advance(session: Session, finding_id: str, *targets: FindingStatus) -> None:
    """Drive the real lifecycle machine toward the given states.

    The service is not involved: this is test setup, and it must exercise the same
    ``FindingLifecycle`` the service uses rather than a shortcut. Targets that are not an edge from
    the finding's current state are skipped, so a caller can name the whole canonical path and have
    the finding walk as far as the graph allows -- the same behaviour ``submit_rebuttal`` relies on.
    """

    lifecycle = FindingLifecycle(session, capability_checker=_AllowAllChecker())
    for target in targets:
        current = FindingRepository(session).get(finding_id)
        assert current is not None
        if target not in allowed_transitions(current.status):
            continue
        lifecycle.transition(
            TransitionRequest(
                finding_id=finding_id,
                target_status=target,
                actor=_ACTOR,
                request_id=f"req-test-{target.value}",
                rationale="test advance",
            )
        )


def _advance_to_verification(session: Session, finding_id: str) -> None:
    """Walk a finding to ``verification_pending``, where a verdict can be recorded."""

    _advance(
        session,
        finding_id,
        FindingStatus.EVIDENCE_PENDING,
        FindingStatus.CHALLENGED,
        FindingStatus.REBUTTAL_PENDING,
        FindingStatus.VERIFICATION_PENDING,
    )


def test_rebuttal_appends_and_does_not_overwrite_the_original_finding(
    client: TestClient, session_factory: object
) -> None:
    finding_id, _claim_id, evidence_id = _seed(client)

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        before = _finding(session, finding_id)
        immutable_before = (
            before.title,
            before.summary,
            before.reasoning,
            list(before.evidence_ids),
            list(before.supporting_evidence_ids),
        )
        assert before.counterevidence_ids == []

        rebuttal = _service(session).submit_rebuttal(
            finding_id=finding_id,
            actor=_ACTOR,
            submitted_by="sys_dispute_sys",
            argument="The attached report covers commit abc123, not def456.",
            evidence_ids=[evidence_id],
            requested_outcome=RebuttalRequestedOutcome.WITHDRAWN,
        )

        after = _finding(session, finding_id)
        immutable_after = (
            after.title,
            after.summary,
            after.reasoning,
            list(after.evidence_ids),
            list(after.supporting_evidence_ids),
        )

        # The finding is untouched apart from its status pointer and accumulated
        # counterevidence -- the two things a challenge is allowed to change.
        assert immutable_after == immutable_before
        assert after.status is FindingStatus.REBUTTAL_PENDING
        assert after.counterevidence_ids == [evidence_id]

        history = _service(session).get_history(finding_id=finding_id)
        assert [item.id for item in history.rebuttals] == [rebuttal.id]
        assert history.rebuttals[0].requested_outcome is RebuttalRequestedOutcome.WITHDRAWN
        # The original finding is still present and unchanged in the history view.
        assert history.finding.id == finding_id


def test_a_rebuttal_citing_unknown_evidence_is_refused(
    client: TestClient, session_factory: object
) -> None:
    finding_id, _claim_id, _evidence_id = _seed(client)

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        with pytest.raises(UnknownEvidenceError):
            _service(session).submit_rebuttal(
                finding_id=finding_id,
                actor=_ACTOR,
                submitted_by="sys_dispute_sys",
                argument="Cites evidence that does not exist.",
                evidence_ids=["ev_does_not_exist"],
            )

        history = _service(session).get_history(finding_id=finding_id)
        assert history.rebuttals == []


def test_a_challenged_finding_can_be_rejected_on_counterevidence(
    client: TestClient, session_factory: object
) -> None:
    finding_id, _claim_id, evidence_id = _seed(client)

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        service = _service(session)
        service.submit_rebuttal(
            finding_id=finding_id,
            actor=_ACTOR,
            submitted_by="sys_dispute_sys",
            argument="The counterevidence shows the test ran on the claimed commit.",
            evidence_ids=[evidence_id],
        )
        _advance_to_verification(session, finding_id)

        disposition = service.record_disposition(
            finding_id=finding_id,
            actor=_ACTOR,
            outcome=DispositionOutcome.REJECTED,
            rationale="The premise that verification was missing does not hold.",
            decided_by="principal_operator",
            policy_version="1.0",
            counterevidence_ids=[evidence_id],
        )

        assert _finding(session, finding_id).status is FindingStatus.REJECTED
        assert disposition.outcome is DispositionOutcome.REJECTED
        assert disposition.supersedes_disposition_id is None
        # The challenge is answered: the finding's accumulated counterevidence is the
        # evidence the rebuttal cited, and the history carries both records.
        history = service.get_history(finding_id=finding_id)
        assert history.finding.counterevidence_ids == [evidence_id]
        assert len(history.rebuttals) == 1
        assert history.rebuttals[0].evidence_ids == [evidence_id]
        assert len(history.dispositions) == 1


def test_a_challenged_finding_can_be_marked_inconclusive_on_counterevidence(
    client: TestClient, session_factory: object
) -> None:
    finding_id, _claim_id, evidence_id = _seed(client)

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        service = _service(session)
        service.submit_rebuttal(
            finding_id=finding_id,
            actor=_ACTOR,
            submitted_by="sys_dispute_sys",
            argument="The evidence conflicts and cannot be resolved from the record.",
            evidence_ids=[evidence_id],
        )
        _advance_to_verification(session, finding_id)

        service.record_disposition(
            finding_id=finding_id,
            actor=_ACTOR,
            outcome=DispositionOutcome.INCONCLUSIVE,
            rationale="The evidence conflicts; neither the finding nor the rebuttal prevails.",
            decided_by="principal_operator",
            policy_version="1.0",
            counterevidence_ids=[evidence_id],
        )

        assert _finding(session, finding_id).status is FindingStatus.INCONCLUSIVE


def test_a_challenged_finding_cannot_be_dismissed_without_counterevidence(
    client: TestClient, session_factory: object
) -> None:
    """The negative case for scenario 11: a rebuttal is answerable, not a formality."""

    finding_id, _claim_id, _evidence_id = _seed(client)

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        service = _service(session)
        service.submit_rebuttal(
            finding_id=finding_id,
            actor=_ACTOR,
            submitted_by="sys_dispute_sys",
            argument="A challenge with no evidence attached.",
        )
        _advance_to_verification(session, finding_id)

        from nyx.findings.lifecycle import InvalidLifecycleTransitionError

        with pytest.raises(InvalidLifecycleTransitionError, match="counterevidence"):
            service.record_disposition(
                finding_id=finding_id,
                actor=_ACTOR,
                outcome=DispositionOutcome.REJECTED,
                rationale="Dismissing the challenge without weighing anything.",
                decided_by="principal_operator",
                policy_version="1.0",
            )

        # The refusal left the finding where it was.
        assert _finding(session, finding_id).status is FindingStatus.VERIFICATION_PENDING


def test_an_unchallenged_finding_can_still_be_rejected_without_counterevidence(
    client: TestClient, session_factory: object
) -> None:
    """The counterevidence rule must not force a meaningless field on an unchallenged finding."""

    finding_id, _claim_id, _evidence_id = _seed(client)

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        service = _service(session)
        _advance(
            session,
            finding_id,
            FindingStatus.EVIDENCE_PENDING,
            FindingStatus.CHALLENGED,
            FindingStatus.REBUTTAL_PENDING,
            FindingStatus.VERIFICATION_PENDING,
        )

        service.record_disposition(
            finding_id=finding_id,
            actor=_ACTOR,
            outcome=DispositionOutcome.REJECTED,
            rationale="The critic misread the evidence; no counterevidence was needed.",
            decided_by="principal_operator",
            policy_version="1.0",
        )

        assert _finding(session, finding_id).status is FindingStatus.REJECTED


def test_a_new_disposition_supersedes_the_previous_one_and_both_remain_readable(
    client: TestClient, session_factory: object
) -> None:
    finding_id, _claim_id, _evidence_id = _seed(client)

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        service = _service(session)
        _advance_to_verification(session, finding_id)
        first = service.record_disposition(
            finding_id=finding_id,
            actor=_ACTOR,
            outcome=DispositionOutcome.REJECTED,
            rationale="First disposition.",
            decided_by="principal_operator",
            policy_version="1.0",
        )
        # A second disposition is only reachable from a state that allows it; reopen via
        # remediation, which is the documented route back.
        _advance(session, finding_id, FindingStatus.REMEDIATION_PENDING)
        second = service.record_disposition(
            finding_id=finding_id,
            actor=_ACTOR,
            outcome=DispositionOutcome.ACCEPTED_RISK,
            rationale="Second disposition supersedes the first: risk accepted instead.",
            decided_by="principal_auditor",
            policy_version="1.1",
        )

        assert second.supersedes_disposition_id == first.id
        chain = service.disposition_chain(disposition_id=second.id)
        assert [item.id for item in chain] == [first.id, second.id]
        history = service.get_history(finding_id=finding_id)
        assert [item.id for item in history.chain] == [first.id, second.id]
        assert history.current_disposition is not None
        assert history.current_disposition.id == second.id


def test_a_principal_without_the_disposition_capability_cannot_record_one(
    client: TestClient, session_factory: object
) -> None:
    finding_id, _claim_id, _evidence_id = _seed(client)

    with session_scope(session_factory) as session:  # type: ignore[arg-type]
        # Default checker is deny-all, so no capability is granted.
        service = DisputeService(session, clock=lambda: _NOW)
        _advance_to_verification(session, finding_id)

        with pytest.raises(CapabilityDeniedError):
            service.record_disposition(
                finding_id=finding_id,
                actor=_ACTOR,
                outcome=DispositionOutcome.CONFIRMED,
                rationale="Attempted without the disposition capability.",
                decided_by="principal_operator",
                policy_version="1.0",
            )
