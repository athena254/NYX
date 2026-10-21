"""Unit tests for the Git/CI normalization slice (Phase 5, slice 5.2).

The contract is ``docs/implementation/phase-5-plan.md`` §2.4: a GitHub payload becomes
``EventInput`` / ``EvidenceInput`` models with ``scope.commit`` set from the run's head SHA,
``trust_class`` ``observed``, a ``ci_test_result`` evidence item whose text carries the
conclusion GitHub actually reported, and an ``access_policy_ref`` the evidence route accepts.

These tests are pure: no network, no database, no adapter config, and no import of
``nyx.adapters.git_ci.config`` / ``github_client`` (a sibling slice owns those).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nyx.adapters.git_ci.normalize import (
    ACCESS_POLICY_REF,
    EVENT_TYPE_CHECK,
    EVENT_TYPE_COMMIT,
    EVENT_TYPE_WORKFLOW,
    NYX_PRODUCER_SYSTEM_ID,
    NormalizationError,
    normalize_actions_run,
    normalize_check_run,
    normalize_commits,
    provider_event_id,
)
from nyx.domain.base import PrincipalRef, Scope
from nyx.domain.enums import (
    EvidenceKind,
    IntegrityMethod,
    PrincipalKind,
    RetentionClass,
    Sensitivity,
    TrustClass,
)
from nyx.ingestion.inputs import EventInput, EvidenceInput

REPO = "owner/repo"

#: The ``evidence:*`` read values ``api/routes/evidence.py`` accepts, plus the restricted one.
#: An unknown value in that namespace is refused at registration, so this is a closed set.
ACCEPTED_POLICY_REFS = {
    "evidence:read",
    "evidence:internal",
    "evidence:public",
    "evidence:standard",
    "evidence:restricted",
}

#: ``EventType``'s pattern, copied from ``src/nyx/domain/events.py`` so a domain change is
#: visible here rather than hidden.
EVENT_TYPE_PATTERN = re.compile(r"^[a-z0-9]+(\.[a-z0-9][a-z0-9_-]*)+$")

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
COMPLETED_AT = "2026-10-19T10:00:00Z"
STARTED_AT = "2026-10-09T09:00:00Z"


def check_run(**overrides: object) -> dict[str, object]:
    """A real-shaped GitHub check-run payload (the documented ``check-runs`` object)."""

    run: dict[str, object] = {
        "id": 12345,
        "head_sha": HEAD_SHA,
        "name": "build-and-test",
        "status": "completed",
        "conclusion": "success",
        "check_suite": {"id": 67890, "display_title": "CI"},
        "html_url": f"https://github.com/{REPO}/runs/12345",
        "started_at": STARTED_AT,
        "completed_at": COMPLETED_AT,
        "actor": {"login": "octocat", "id": 583231},
    }
    run.update(overrides)
    return run


def actions_run(**overrides: object) -> dict[str, object]:
    """A real-shaped GitHub Actions workflow-run payload."""

    run: dict[str, object] = {
        "id": 112233,
        "name": "Release",
        "head_branch": "main",
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "success",
        "html_url": f"https://github.com/{REPO}/actions/runs/112233",
        "run_started_at": STARTED_AT,
        "created_at": STARTED_AT,
        "updated_at": COMPLETED_AT,
        "triggering_actor": {"login": "octocat", "id": 583231},
    }
    run.update(overrides)
    return run


def commit(**overrides: object) -> dict[str, object]:
    """A real-shaped GitHub commit object."""

    value: dict[str, object] = {
        "sha": HEAD_SHA,
        "html_url": f"https://github.com/{REPO}/commit/{HEAD_SHA}",
        "author": {"login": "octocat", "date": COMPLETED_AT},
        "committer": {"login": "octocat", "date": COMPLETED_AT},
        "commit": {
            "message": "Fix the release pipeline",
            "timestamp": COMPLETED_AT,
            "distinct": True,
            "verification": {"verified": False},
        },
    }
    value.update(overrides)
    return value


def test_check_run_produces_one_event_and_one_ci_test_result() -> None:
    records = normalize_check_run(check_run(), repository=REPO)

    assert len(records) == 2
    event = next(r for r in records if isinstance(r, EventInput))
    evidence = next(r for r in records if isinstance(r, EvidenceInput))

    assert event.event_type == EVENT_TYPE_CHECK
    assert event.event_type == "ci.check.completed"
    assert EVENT_TYPE_PATTERN.match(event.event_type)
    # ``EventInput`` has no scope field, so the head SHA rides in the payload; the evidence
    # -- the record C-001 compares -- carries it in ``scope.commit``.
    assert event.payload["commit"] == HEAD_SHA
    assert event.correlation_id == "12345"
    assert event.trust_class == TrustClass.OBSERVED
    assert event.occurred_at == datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    assert event.producer_event_id == "gh:owner/repo:check:12345"

    assert evidence.kind == EvidenceKind.CI_TEST_RESULT
    assert evidence.kind == "ci_test_result"
    assert evidence.scope == Scope(system_id=NYX_PRODUCER_SYSTEM_ID, commit=HEAD_SHA)
    assert evidence.scope is not None and evidence.scope.commit == HEAD_SHA
    assert evidence.integrity_method == IntegrityMethod.HASH
    assert evidence.sensitivity == Sensitivity.INTERNAL
    assert evidence.retention_class == RetentionClass.STANDARD
    assert evidence.locator == "job:build-and-test"
    assert evidence.source_ref == f"https://github.com/{REPO}/runs/12345"
    # C-001 reads the outcome with `\bpass(?:es|ed|ing)?\b|\bgreen\b` -- the conclusion must be
    # present as a bare word.
    assert re.search(r"\bsuccess\b", f"{evidence.title} {evidence.description}")
    assert evidence.limitations


def test_normalization_is_stable_across_calls() -> None:
    payload = check_run()
    first = normalize_check_run(payload, repository=REPO)
    second = normalize_check_run(payload, repository=REPO)

    assert [r.model_dump(mode="json") for r in first] == [r.model_dump(mode="json") for r in second]


def test_provider_event_id_is_stable_and_distinct_per_run() -> None:
    same = [provider_event_id("check", REPO, "12345") for _ in range(2)]
    assert same[0] == same[1] == "gh:owner/repo:check:12345"
    assert provider_event_id("check", REPO, "12345") != provider_event_id("check", REPO, "12346")
    assert provider_event_id("run", REPO, "12345") != provider_event_id("check", REPO, "12345")


def test_unknown_extra_fields_are_ignored() -> None:
    payload = check_run()
    payload["some_new_github_field"] = {"nested": [1, 2, 3]}
    payload["another"] = "value"

    records = normalize_check_run(payload, repository=REPO)

    assert len(records) == 2
    assert all(isinstance(r, (EventInput, EvidenceInput)) for r in records)


def test_missing_head_sha_raises_rather_than_emitting_an_unscoped_event() -> None:
    for bad in ({"head_sha": None}, {"head_sha": ""}, {}):
        payload = check_run(**bad) if bad else check_run()
        if not bad:
            del payload["head_sha"]
        with pytest.raises(NormalizationError) as excinfo:
            normalize_check_run(payload, repository=REPO)
        assert "head_sha" in str(excinfo.value)


def test_actions_run_maps_to_workflow_event_type() -> None:
    records = normalize_actions_run(actions_run(), repository=REPO)

    event = next(r for r in records if isinstance(r, EventInput))
    evidence = next(r for r in records if isinstance(r, EvidenceInput))

    assert event.event_type == EVENT_TYPE_WORKFLOW
    assert event.event_type == "ci.workflow.completed"
    assert EVENT_TYPE_PATTERN.match(event.event_type)
    assert event.payload["commit"] == HEAD_SHA
    assert event.correlation_id == "112233"
    assert event.trust_class == TrustClass.OBSERVED
    assert evidence.kind == "ci_test_result"
    assert evidence.scope is not None and evidence.scope.commit == HEAD_SHA


def test_trust_class_is_observed_never_verified() -> None:
    for factory in (normalize_check_run, normalize_actions_run):
        records = factory(
            check_run() if factory is normalize_check_run else actions_run(), repository=REPO
        )
        event = next(r for r in records if isinstance(r, EventInput))
        assert event.trust_class == TrustClass.OBSERVED
        assert event.trust_class != TrustClass.TOOL_VERIFIED
        assert event.trust_class != TrustClass.SIGNED_ATTESTATION


def test_access_policy_ref_is_one_of_the_five_accepted_values() -> None:
    assert ACCESS_POLICY_REF in ACCEPTED_POLICY_REFS
    for factory, payload in (
        (normalize_check_run, check_run()),
        (normalize_actions_run, actions_run()),
    ):
        records = factory(payload, repository=REPO)
        evidence = next(r for r in records if isinstance(r, EvidenceInput))
        assert evidence.access_policy_ref in ACCEPTED_POLICY_REFS


def test_commit_with_no_usable_observation_returns_empty_list() -> None:
    assert normalize_commits([], repository=REPO) == []
    assert normalize_commits([{"sha": ""}], repository=REPO) == []
    assert normalize_commits([{"sha": None}], repository=REPO) == []
    # A commit object with no timestamp anywhere is not usable either.
    assert normalize_commits([{"sha": HEAD_SHA, "commit": {}}], repository=REPO) == []


def test_commit_with_no_observation_does_not_raise() -> None:
    # Rule 8: an unusable observation returns [], it does not raise.
    queued = {
        "id": 1,
        "name": "build-and-test",
        "status": "queued",
        "head_sha": HEAD_SHA,
        "started_at": STARTED_AT,
        "conclusion": None,
    }
    in_progress = {**queued, "status": "in_progress"}
    assert normalize_check_run(queued, repository=REPO) == []
    assert normalize_check_run(in_progress, repository=REPO) == []


def test_commits_normalize_to_commit_observed_events() -> None:
    events = normalize_commits([commit()], repository=REPO)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == EVENT_TYPE_COMMIT
    assert event.event_type == "commit.observed"
    assert event.correlation_id == HEAD_SHA
    assert event.producer_event_id == f"gh:{REPO}:commit:{HEAD_SHA}"
    assert event.trust_class == TrustClass.OBSERVED
    assert event.occurred_at == datetime(2026, 10, 19, 10, 0, tzinfo=UTC)


def test_actor_is_a_tool_principal_and_is_omitted_when_absent() -> None:
    event = normalize_check_run(check_run(), repository=REPO)[0]
    assert isinstance(event, EventInput)
    assert event.actor == PrincipalRef(
        id="gh:octocat", kind=PrincipalKind.TOOL, display_name="octocat"
    )
    assert event.actor is not None and event.actor.kind == PrincipalKind.TOOL

    without_actor = check_run(actor=None)
    event = normalize_check_run(without_actor, repository=REPO)[0]
    assert isinstance(event, EventInput)
    assert event.actor is None


def test_failure_conclusion_is_preserved_without_inventing_a_pass_word() -> None:
    records = normalize_check_run(check_run(conclusion="failure"), repository=REPO)
    evidence = next(r for r in records if isinstance(r, EvidenceInput))

    text = f"{evidence.title} {evidence.description}"
    assert re.search(r"\bfailure\b", text)
    # `passed` must not appear: the source said failure, and "success"/"passed" are not added.
    assert not re.search(r"\bpassed\b|\bsuccess\b", text)


def test_unknown_conclusion_is_preserved_and_gets_no_pass_or_fail_word() -> None:
    records = normalize_check_run(check_run(conclusion="neutral"), repository=REPO)
    evidence = next(r for r in records if isinstance(r, EvidenceInput))

    text = f"{evidence.title} {evidence.description}"
    assert re.search(r"\bneutral\b", text)
    assert not re.search(r"\bpassed\b|\bfailed\b", text)
    assert evidence.limitations


def test_malformed_payload_raises_a_typed_error_naming_the_field() -> None:
    cases = [
        (check_run(id=None), "id"),
        (check_run(name=""), "name"),
        (check_run(completed_at=None, started_at=None), "completed_at"),
    ]
    for payload, field in cases:
        with pytest.raises(NormalizationError) as excinfo:
            normalize_check_run(payload, repository=REPO)
        assert field in str(excinfo.value)

    with pytest.raises(NormalizationError):
        normalize_check_run([], repository=REPO)  # type: ignore[arg-type]
    with pytest.raises(NormalizationError):
        normalize_check_run(check_run(), repository="")  # type: ignore[arg-type]


def test_output_validates_as_the_gateway_input_models() -> None:
    for records in (
        normalize_check_run(check_run(), repository=REPO),
        normalize_actions_run(actions_run(), repository=REPO),
    ):
        for record in records:
            if isinstance(record, EventInput):
                assert EventInput.model_validate(record.model_dump(mode="json")).event_type
            else:
                assert EvidenceInput.model_validate(record.model_dump(mode="json")).kind


def test_module_imports_standalone_from_its_own_file() -> None:
    """Load ``normalize.py`` by path, bypassing the package ``__init__``.

    ``src/nyx/adapters/git_ci/__init__.py`` is slice 5.1's and imports ``config`` and
    ``github_client``. Executing it is what would couple this module to its siblings, so the
    module is loaded directly from its file here: if it needed either sibling, this would fail.
    """
    import importlib.util

    path = Path("src/nyx/adapters/git_ci/normalize.py")
    spec = importlib.util.spec_from_file_location("normalize_standalone", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    records = module.normalize_check_run(check_run(), repository=REPO)
    assert len(records) == 2
    assert module.provider_event_id("check", REPO, "12345") == "gh:owner/repo:check:12345"


def test_module_imports_standalone_without_the_sibling_slices() -> None:
    # The slice must not depend on config.py / github_client.py (written by another agent).
    source = Path("src/nyx/adapters/git_ci/normalize.py").read_text(encoding="utf-8")
    assert "from nyx.adapters.git_ci.config" not in source
    assert "from nyx.adapters.git_ci.github_client" not in source
    for forbidden in ("subprocess", "os.system", "shell=True", "eval(", "exec("):
        assert forbidden not in source


def test_module_does_not_import_the_deterministic_core() -> None:
    # Parse the imports rather than scanning the file text: the docstring legitimately names
    # the modules this boundary forbids, and a text grep would flag that as a violation.
    import ast

    tree = ast.parse(Path("src/nyx/adapters/git_ci/normalize.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for forbidden in (
        "nyx.critics",
        "nyx.findings",
        "nyx.disputes",
        "nyx.policies",
        "nyx.storage",
        "nyx.retention",
        "nyx.auth",
        "nyx.systems",
        "nyx.audit",
    ):
        assert not any(name == forbidden or name.startswith(forbidden + ".") for name in imported)


def test_module_does_not_import_the_sibling_slices() -> None:
    # Slice 5.1 owns config.py / github_client.py; this slice must not depend on them, so the
    # module stays importable standalone while both are written concurrently.
    import ast

    tree = ast.parse(Path("src/nyx/adapters/git_ci/normalize.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("nyx.adapters.git_ci"), node.module
