"""HTTP surface for the dispute protocol (SPEC §9.1, §5.7).

Registering a route is not the same as the route working, so these tests exercise the endpoints
end to end against a migrated database: the request bodies, the error envelope, and the state the
finding ends up in.

The capability seam is tested both ways. The application's default is deny-all, so a disposition is
refused with ``CAPABILITY_REQUIRED``; injecting an allow-all checker (the Phase 4 seam) lets one
succeed. That pair proves the seam is real and wireable rather than decorative.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from nyx.api.app import create_app
from nyx.config import Settings
from nyx.domain.base import PrincipalRef
from nyx.storage.db import SessionFactory

API = "/api/v1"
_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class _AllowAllChecker:
    """Test double for the Phase-4 seam: grants every capability."""

    def has_capability(self, principal: PrincipalRef, capability: object) -> bool:
        return True


def _seed(
    client: TestClient, evidence_commit: str = "def456", headers: dict[str, str] | None = None
) -> tuple[str, str, str]:
    """Register a system, claim, event and evidence; run C-001; return ids.

    ``headers`` carries the caller's credential when the app authenticates its callers.
    """

    sys_id = client.post(
        f"{API}/systems",
        json={
            "name": "api-dispute-sys",
            "kind": "agent",
            "environment": "development",
            "owner": "test",
            "adapter": "http",
        },
        headers=headers,
    ).json()["id"]

    claim_id = client.post(
        f"{API}/claims",
        json={
            "statement": "All required tests pass for commit abc123.",
            "claimant_system_id": sys_id,
            "claim_type": "completion",
            "scope": {"system_id": sys_id, "artifact": "demo", "commit": "abc123"},
            "producer_event_id": "a-claim-1",
        },
        headers=headers,
    ).json()["id"]

    event_id = client.post(
        f"{API}/events",
        json={
            "source_system_id": sys_id,
            "event_type": "agent.completion_claim",
            "occurred_at": "2026-10-19T10:00:00Z",
            "correlation_id": "a-corr-1",
            "payload": {"claim_id": claim_id},
            "producer_event_id": "a-evt-1",
            "trust_class": "observed",
            "labels": ["api-dispute"],
            "retention_class": "standard",
        },
        headers=headers,
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
            "producer_event_id": "a-ev-1",
        },
        headers=headers,
    ).json()["id"]

    run = client.post(
        f"{API}/critics/run",
        json={"event_id": event_id, "critic_ids": ["c001-completion-vs-verification"]},
        headers=headers,
    )
    assert run.status_code == 200, run.text
    finding_id = run.json()["results"][0]["finding_id"]
    assert finding_id
    return finding_id, claim_id, evidence_id


def test_a_rebuttal_can_be_submitted_and_read_back(client: TestClient) -> None:
    finding_id, _claim_id, evidence_id = _seed(client)

    response = client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "sys_api_dispute_sys",
            "argument": "The attached report covers commit abc123, not def456.",
            "evidence_ids": [evidence_id],
            "requested_outcome": "withdrawn",
        },
    )
    assert response.status_code == 200, response.text
    rebuttal = response.json()
    assert rebuttal["finding_id"] == finding_id
    assert rebuttal["requested_outcome"] == "withdrawn"

    # The finding advanced to rebuttal_pending and is otherwise unchanged.
    finding = client.get(f"{API}/findings/{finding_id}").json()
    assert finding["status"] == "rebuttal_pending"
    assert finding["title"], "the original finding title was lost"

    listed = client.get(f"{API}/findings/{finding_id}/rebuttals")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [rebuttal["id"]]


def test_a_rebuttal_citing_unknown_evidence_is_rejected_with_the_structured_envelope(
    client: TestClient,
) -> None:
    finding_id, _claim_id, _evidence_id = _seed(client)

    response = client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "sys_api_dispute_sys",
            "argument": "Cites evidence that does not exist.",
            "evidence_ids": ["ev_does_not_exist"],
        },
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "UNKNOWN_EVIDENCE_ID"
    assert body["request_id"]
    # The refusal wrote nothing.
    assert client.get(f"{API}/findings/{finding_id}/rebuttals").json()["items"] == []


def test_an_unregistered_verification_check_is_refused(client: TestClient) -> None:
    finding_id, _claim_id, _evidence_id = _seed(client)

    response = client.post(
        f"{API}/findings/{finding_id}/verification-jobs",
        json={"check_id": "tests.run-arbitrary-command", "requested_by": "principal_operator"},
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "VERIFICATION_NOT_PERMITTED"


def test_an_allowlisted_verification_check_can_be_queued_and_run(client: TestClient) -> None:
    finding_id, _claim_id, _evidence_id = _seed(client)

    queued = client.post(
        f"{API}/findings/{finding_id}/verification-jobs",
        json={"check_id": "critics.rerun-linked", "requested_by": "principal_operator"},
    )
    assert queued.status_code == 200, queued.text
    job = queued.json()["job"]
    assert job["status"] == "queued"

    ran = client.post(f"{API}/findings/{finding_id}/verification-jobs/run")
    assert ran.status_code == 200, ran.text
    results = ran.json()["items"]
    assert [item["id"] for item in results] == [job["id"]]
    assert results[0]["status"] == "succeeded"
    # The independence limitation travels with the result, so a reader cannot mistake a
    # reproduction check for independent corroboration.
    assert "not independent corroboration" in results[0]["independence"]


def test_history_returns_the_finding_and_everything_appended_to_it(client: TestClient) -> None:
    finding_id, _claim_id, evidence_id = _seed(client)

    client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "sys_api_dispute_sys",
            "argument": "A challenge.",
            "evidence_ids": [evidence_id],
        },
    )

    response = client.get(f"{API}/findings/{finding_id}/history")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["finding"]["id"] == finding_id
    assert len(body["rebuttals"]) == 1
    assert body["verification_jobs"] == []
    assert body["dispositions"] == []
    assert body["current_disposition"] is None


def test_a_disposition_succeeds_for_the_local_operator(client: TestClient) -> None:
    """In ``auth_mode='none'`` the caller is the loopback local operator, which holds every
    capability.

    An earlier version of this test asserted a 403 here, on the premise that the application default
    was deny-all. Phase 4 replaced that default (ADR 0007): loopback-only, no credential, full
    operator capabilities. The declared ``decided_by`` is ``principal_operator`` -- deliberately not
    the local operator's id -- to pin that the identity in the body is no longer what authorises the
    action.
    """

    finding_id, _claim_id, evidence_id = _seed(client)

    client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "principal_operator",
            "argument": "A challenge to answer.",
            "evidence_ids": [evidence_id],
        },
    )

    response = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": "rejected",
            "rationale": "The local operator may record a disposition.",
            "decided_by": "principal_operator",
            "policy_version": "1.0",
            "counterevidence_ids": [evidence_id],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "rejected"


def test_a_disposition_succeeds_once_the_capability_seam_grants_it(client: TestClient) -> None:
    """Proves the seam is wireable: the same request succeeds with a granting checker."""

    finding_id, _claim_id, evidence_id = _seed(client)
    client.app.state.capability_checker = _AllowAllChecker()

    # Walk to verification_pending through the API's own rebuttal path.
    client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "sys_api_dispute_sys",
            "argument": "A challenge to answer.",
            "evidence_ids": [evidence_id],
        },
    )

    response = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": "rejected",
            "rationale": "The premise that verification was missing does not hold.",
            "decided_by": "principal_operator",
            "policy_version": "1.0",
            "counterevidence_ids": [evidence_id],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "rejected"

    history = client.get(f"{API}/findings/{finding_id}/history").json()
    assert history["finding"]["status"] == "rejected"
    assert len(history["dispositions"]) == 1
    assert history["current_disposition"]["id"] == history["dispositions"][0]["id"]


def test_an_unknown_finding_is_a_404_with_the_structured_envelope(client: TestClient) -> None:
    response = client.get(f"{API}/findings/fnd_missing/history")
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------- Phase 4 authorisation
#
# The four tests below prove the capability seam is real against an *authenticated* caller, which
# the tests above cannot: they run in auth_mode='none', where the caller is the loopback local
# operator by construction (ADR 0007).


_AUDITOR = {
    "principal_id": "auditor",
    "api_key_env": "NYX_PROBE_KEY_AUDITOR",
    "key": "probe-key-auditor-value",
    "capabilities": ["evidence.read", "evidence.restricted"],
}
_OPERATOR = {
    "principal_id": "operator",
    "api_key_env": "NYX_PROBE_KEY_OPERATOR",
    "key": "probe-key-operator-value",
    "capabilities": ["findings.disposition", "evidence.read", "evidence.restricted"],
}


@contextmanager
def _api_key_client(
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Build a client whose deployment authenticates callers with two configured principals."""

    for principal in (_AUDITOR, _OPERATOR):
        monkeypatch.setenv(principal["api_key_env"], principal["key"])
    api_settings = Settings(
        database_url=settings.database_url,
        data_dir=settings.data_dir,
        auth_mode="api_key",
        auth_principals=[
            {
                "principal_id": principal["principal_id"],
                "api_key_env": principal["api_key_env"],
                "capabilities": principal["capabilities"],
                "description": principal["principal_id"],
            }
            for principal in (_AUDITOR, _OPERATOR)
        ],
    )
    app = create_app(api_settings, engine=engine, session_factory=session_factory)
    with TestClient(app) as test_client:
        yield test_client


def _auth(principal: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {principal['key']}"}


def test_an_unauthenticated_caller_is_refused_in_api_key_mode(
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential is a 401, and it never degrades to the local-operator identity."""

    with _api_key_client(settings, engine, session_factory, monkeypatch) as client:
        response = client.post(
            f"{API}/findings/fnd_whatever/dispositions",
            json={
                "outcome": "rejected",
                "rationale": "No credential presented.",
                "decided_by": "operator",
                "policy_version": "1.0",
            },
        )

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "UNAUTHENTICATED"


def test_a_principal_without_the_capability_is_refused(
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auditor can read and seed but cannot record a disposition."""

    with _api_key_client(settings, engine, session_factory, monkeypatch) as client:
        headers = _auth(_AUDITOR)
        finding_id, _claim_id, _evidence_id = _seed(client, headers=headers)

        response = client.post(
            f"{API}/findings/{finding_id}/dispositions",
            json={
                "outcome": "rejected",
                "rationale": "The auditor holds no disposition capability.",
                "decided_by": "auditor",
                "policy_version": "1.0",
            },
            headers=headers,
        )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "CAPABILITY_REQUIRED"


def test_a_caller_cannot_record_an_action_under_another_identitys_name(
    settings: Settings,
    engine: Engine,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actor is the authenticated principal, so a claimed identity grants nothing.

    This is the property that makes wiring the checker safe. Without it, an unprivileged caller
    could put ``decided_by: "operator"`` in the body and be granted the operator's capability.
    """

    with _api_key_client(settings, engine, session_factory, monkeypatch) as client:
        headers = _auth(_AUDITOR)
        finding_id, _claim_id, _evidence_id = _seed(client, headers=headers)

        # The body claims to be the operator. The actor is still the auditor.
        response = client.post(
            f"{API}/findings/{finding_id}/dispositions",
            json={
                "outcome": "rejected",
                "rationale": "Attempted impersonation of the operator identity.",
                "decided_by": "operator",
                "policy_version": "1.0",
            },
            headers=headers,
        )

        assert response.status_code == 403, response.text
        assert response.json()["code"] == "CAPABILITY_REQUIRED"

        # The same request as the real operator succeeds, so the refusal above was the capability
        # and not the lifecycle state.
        client.post(
            f"{API}/findings/{finding_id}/rebuttals",
            json={
                "submitted_by": "operator",
                "argument": "A challenge to answer.",
                "evidence_ids": [_evidence_id],
            },
            headers=_auth(_OPERATOR),
        )
        allowed = client.post(
            f"{API}/findings/{finding_id}/dispositions",
            json={
                "outcome": "rejected",
                "rationale": "The operator holds the disposition capability.",
                "decided_by": "operator",
                "policy_version": "1.0",
                "counterevidence_ids": [_evidence_id],
            },
            headers=_auth(_OPERATOR),
        )

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["outcome"] == "rejected"


def test_the_local_operator_may_record_a_disposition(client: TestClient) -> None:
    """In auth_mode='none' the caller is the local operator, which holds every capability.

    This is ADR 0007's trust decision, and it is what keeps every existing local caller and probe
    working: loopback-only, no credential, full operator capabilities.
    """

    finding_id, _claim_id, evidence_id = _seed(client)

    client.post(
        f"{API}/findings/{finding_id}/rebuttals",
        json={
            "submitted_by": "principal_cli",
            "argument": "A challenge to answer.",
            "evidence_ids": [evidence_id],
        },
    )

    response = client.post(
        f"{API}/findings/{finding_id}/dispositions",
        json={
            "outcome": "rejected",
            "rationale": "The local operator may record a disposition.",
            "decided_by": "principal_cli",
            "policy_version": "1.0",
            "counterevidence_ids": [evidence_id],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "rejected"
