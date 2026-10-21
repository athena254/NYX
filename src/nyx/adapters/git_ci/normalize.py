"""Normalize GitHub CI payloads into NYX producer inputs (Phase 5, slice 5.2).

This module is the **pure** half of the Git/CI adapter: it turns a decoded GitHub API response
(a plain ``dict``) into the producer input models the ingestion gateway already understands
(``EventInput``, ``EvidenceInput``). It performs no I/O, holds no state, and reads nothing
from the environment.

The domain mapping is the contract in ``docs/implementation/phase-5-plan.md`` §2.4:

=========================  ================================================================
External thing            NYX record
=========================  ================================================================
Commit SHA (head SHA)     ``EvidenceInput.scope.commit`` -- this is the field C-001 compares
                          (``c001_completion_vs_verification.py``, ``_covers``). The event
                          carries it in ``payload["commit"]``; see the note below.
Check run / Actions run   ``Event`` with ``event_type`` ``ci.check.completed`` /
                          ``ci.workflow.completed``, ``correlation_id`` = the external
                          run id, ``trust_class`` = ``observed``
Run conclusion            ``EvidenceInput`` with ``kind`` ``ci_test_result``,
                          ``title`` = job name, ``description`` = conclusion + suite
=========================  ================================================================

**One deliberate deviation from the plan's §2.4 table.** The plan says to set the head SHA
"on the event's ``Scope``". ``EventInput`` has **no** ``scope`` field
(``src/nyx/ingestion/inputs.py``) and is ``extra="forbid"``, so that cannot be done literally;
adding a field is a core-model change, which is out of scope for this slice. The head SHA is
therefore carried in ``payload["commit"]``, and the *evidence* -- the record C-001 actually
compares against -- carries it in ``scope.commit`` exactly as the plan specifies. A missing
head SHA still raises, because a record with no commit cannot be scoped.

Three rules are enforced here because violating them would silently corrupt the record rather
than fail loudly:

* **A run with no head SHA raises.** ``scope.commit`` is the field a critic correlates on.
  Emitting an event with an empty commit would produce an unscoped finding, which is worse
  than no finding.
* **``trust_class`` is ``observed``.** A GitHub API response establishes that GitHub
  *recorded* the run; it does not establish that the pipeline was not influenced by the same
  actor whose claim is under assessment (SPEC §8, correlated sources). ``tool_verified`` and
  ``signed_attestation`` would claim exactly that.
* **No pass/fail word is invented.** C-001 reads the outcome from the evidence title and
  description with word-boundary markers. The conclusion is stated in the words the host
  used; a ``neutral`` conclusion is *not* rewritten as ``passed`` or ``failed``.

Unknown fields are ignored. GitHub adds fields over time and a strict reader would break on
every one; only a missing *required* field raises.

This module imports only ``nyx.domain`` and ``nyx.ingestion.inputs`` plus the standard
library (the adapter boundary, ``docs/architecture.md`` §3). It imports nothing from
``nyx.critics``, ``nyx.findings``, ``nyx.disputes``, ``nyx.policies``, ``nyx.storage``,
``nyx.retention``, ``nyx.auth``, ``nyx.systems``, or ``nyx.audit``, and nothing from its own
sibling modules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

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

#: The adapter's own service identity, exactly as ``ingestion/gateway.py`` records a producer.
#: It is a constant, never a configured value: the adapter does not invent an operator.
NYX_PRODUCER_SYSTEM_ID = "git-ci"

#: The access policy the evidence route accepts (``api/routes/evidence.py`` treats the
#: ``evidence:`` namespace as strict and refuses an unknown value at registration).
ACCESS_POLICY_REF = "evidence:internal"

#: The event types this adapter emits. Each matches ``EventType``'s pattern
#: ``^[a-z0-9]+(\.[a-z0-9][a-z0-9_-]*)+$``.
EVENT_TYPE_CHECK = "ci.check.completed"
EVENT_TYPE_WORKFLOW = "ci.workflow.completed"
EVENT_TYPE_COMMIT = "commit.observed"

#: The honest default limitation: ``EvidenceInput.limitations`` has ``min_length=1``.
DEFAULT_LIMITATION = (
    "GitHub's record of the run; the pipeline may have been influenced by the same actor "
    "whose claim is under assessment, and the record establishes the run happened, not "
    "that the claim is true."
)
#: Stated once per evidence item when the adapter cannot derive a pass/fail word at all.
NO_OUTCOME_LIMITATION = (
    "The host reported no conclusion this adapter can map to a test outcome, so the recorded "
    "outcome is not stated in this evidence."
)

#: Why this evidence is unlinked at registration. ``EvidenceItem`` requires a relevance target
#: or an explanation (SPEC §5.4), and an observing adapter produces evidence *before* any
#: claim references the run -- so the link is established later, by whoever files the claim,
#: not by the adapter. Stated in the record rather than left for a reader to infer.
UNLINKED_RELEVANCE_NOTE = (
    "Produced by the git-ci adapter from a CI run. Not linked to a claim at registration: "
    "the adapter observes the run before any claim references it, so the link is established "
    "when a claim citing this commit is assessed."
)

#: GitHub's vocabulary for a run ``status``. Only a completed run carries a conclusion.
_COMPLETED_STATUS = "completed"
#: GitHub's vocabulary for a run ``conclusion`` (lowercased).
_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "completed",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "success",
        "timed_out",
    }
)

#: Field-length ceilings, all well under the model limits they feed.
_MAX_TEXT = 200
_MAX_CONCLUSION = 32
_MAX_EXTERNAL_ID = 100


class NormalizationError(ValueError):
    """A payload is malformed: a *required* field is missing, empty, or the wrong type.

    ``field`` names the offending payload key and the message says why. Raising this -- rather
    than a bare ``KeyError``/``TypeError`` traceback -- is what makes a rejected record a
    structured refusal instead of a crash, and is what the gateway reports as
    ``SCHEMA_VALIDATION_FAILED`` with a field pointer.

    Unknown *extra* keys are not an error and never raise.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def provider_event_id(kind: str, repository: str, external_id: str) -> str:
    """Return the stable ``producer_event_id`` for one external CI object.

    ``gh:{repository}:{kind}:{external_id}`` -- e.g. ``gh:owner/repo:check:12345``. The id is a
    pure function of the external object's own identity, so refetching the same run yields the
    same key and the ingestion gateway reports ``DUPLICATE_EVENT`` rather than writing a second
    record; two different runs can never collide.

    Raises :class:`NormalizationError` when any component is unusable: an id built from an
    empty run id would collapse every run in the repository onto one key.
    """

    for name, value in (("kind", kind), ("repository", repository), ("external_id", external_id)):
        if not isinstance(value, str) or not value.strip():
            raise NormalizationError(f"{name} must be a non-empty string", field=name)
    if len(external_id) > _MAX_EXTERNAL_ID:
        raise NormalizationError(
            f"external_id is longer than {_MAX_EXTERNAL_ID} characters", field="external_id"
        )
    return f"gh:{repository}:{kind}:{external_id}"


def normalize_check_run(
    run: dict[str, Any], *, repository: str
) -> list[EventInput | EvidenceInput]:
    """Normalize one GitHub **check run** (``GET .../commits/{ref}/check-runs``) into inputs.

    Returns ``[EventInput, EvidenceInput]`` for a usable observation, and ``[]`` for a run
    that carries no usable observation -- a status that is not ``completed`` and no conclusion
    -- rather than emitting an empty event. Raises :class:`NormalizationError` for a missing
    head SHA, a missing name, a missing timestamp, or a payload of the wrong shape.
    """

    _validate_shape(run, repository)
    conclusion = _conclusion(run)
    state = _state(run)
    if not _has_usable_observation(state, conclusion):
        return []

    commit = _require_head_sha(run)
    name = _require_name(run)
    occurred_at = _require_timestamp(run, "check")
    _validate_field_lengths(run)
    actor = _run_actor(run)
    url = _html_url(run, repository, "check-runs", run.get("id"))
    suite = _suite(run)

    event = EventInput(
        source_system_id=NYX_PRODUCER_SYSTEM_ID,
        event_type=EVENT_TYPE_CHECK,
        occurred_at=occurred_at,
        correlation_id=str(run["id"]),
        actor=actor,
        payload={
            "check_run_id": run["id"],
            "name": name,
            "status": state,
            "conclusion": conclusion,
            "repository": repository,
            "suite": suite,
            # ``EventInput`` has no ``scope`` field, so the head SHA -- the value C-001
            # correlates on -- is carried here rather than being dropped. See the module
            # docstring: this is the one place the plan's §2.4 event mapping cannot be literal.
            "commit": commit,
        },
        source_ref=url,
        trust_class=TrustClass.OBSERVED,
        labels=[],
        retention_class=RetentionClass.STANDARD,
        producer_event_id=provider_event_id("check", repository, str(run["id"])),
    )
    evidence = EvidenceInput(
        kind=EvidenceKind.CI_TEST_RESULT,
        title=name,
        description=_evidence_description(name, conclusion, suite, state),
        source_system_id=NYX_PRODUCER_SYSTEM_ID,
        source_ref=url,
        locator=f"job:{name}",
        scope=Scope(system_id=NYX_PRODUCER_SYSTEM_ID, commit=commit),
        captured_at=occurred_at,
        valid_from=occurred_at,
        integrity_method=IntegrityMethod.HASH,
        producer_identity=_producer_identity(),
        limitations=_limitations(conclusion),
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref=ACCESS_POLICY_REF,
        retention_class=RetentionClass.STANDARD,
        producer_event_id=provider_event_id("check-evidence", repository, str(run["id"])),
        relevance_note=UNLINKED_RELEVANCE_NOTE,
    )
    return [event, evidence]


def normalize_actions_run(
    run: dict[str, Any], *, repository: str
) -> list[EventInput | EvidenceInput]:
    """Normalize one GitHub **Actions workflow run** (``GET .../actions/runs``) into inputs.

    Same shape and the same rules as :func:`normalize_check_run`, with
    ``event_type="ci.workflow.completed"`` and the ``gh:...:run:`` producer key. An Actions run
    reports a workflow conclusion rather than a per-job test result, so the evidence title
    states the workflow and the branch, and the branch is recorded in the event payload as
    ``head_branch``.
    """

    _validate_shape(run, repository)
    conclusion = _conclusion(run)
    state = _state(run)
    if not _has_usable_observation(state, conclusion):
        return []

    commit = _require_head_sha(run)
    name = _require_name(run)
    occurred_at = _require_timestamp(run, "actions")
    _validate_field_lengths(run)
    actor = _run_actor(run)
    url = _html_url(run, repository, "actions/runs", run.get("id"))
    branch = _head_branch(run)

    event = EventInput(
        source_system_id=NYX_PRODUCER_SYSTEM_ID,
        event_type=EVENT_TYPE_WORKFLOW,
        occurred_at=occurred_at,
        correlation_id=str(run["id"]),
        actor=actor,
        payload={
            "run_id": run["id"],
            "name": name,
            "status": state,
            "conclusion": conclusion,
            "repository": repository,
            "head_branch": branch,
            # See the check-run path: ``EventInput`` has no ``scope`` field.
            "commit": commit,
        },
        source_ref=url,
        trust_class=TrustClass.OBSERVED,
        labels=[],
        retention_class=RetentionClass.STANDARD,
        producer_event_id=provider_event_id("run", repository, str(run["id"])),
    )
    evidence = EvidenceInput(
        kind=EvidenceKind.CI_TEST_RESULT,
        title=_workflow_title(name, branch),
        description=_workflow_description(name, conclusion, branch, state),
        source_system_id=NYX_PRODUCER_SYSTEM_ID,
        source_ref=url,
        locator=f"job:{name}",
        scope=Scope(system_id=NYX_PRODUCER_SYSTEM_ID, commit=commit),
        captured_at=occurred_at,
        valid_from=occurred_at,
        integrity_method=IntegrityMethod.HASH,
        producer_identity=_producer_identity(),
        limitations=_limitations(conclusion),
        sensitivity=Sensitivity.INTERNAL,
        access_policy_ref=ACCESS_POLICY_REF,
        retention_class=RetentionClass.STANDARD,
        producer_event_id=provider_event_id("run-evidence", repository, str(run["id"])),
        relevance_note=UNLINKED_RELEVANCE_NOTE,
    )
    return [event, evidence]


def normalize_commits(commits: list[dict[str, Any]], *, repository: str) -> list[EventInput]:
    """Normalize GitHub commit objects into ``commit.observed`` events.

    A commit is an observation of the repository's history, not a verification: there is no
    conclusion, so there is no evidence item. A commit with no usable observation (no sha, or
    no parseable timestamp, or an empty ``commit`` payload) returns ``[]`` for that commit
    rather than emitting an empty event.
    """

    if not isinstance(commits, list):
        raise NormalizationError("commits must be a list of objects", field="commits")
    if not isinstance(repository, str) or not repository.strip():
        raise NormalizationError("repository must be a non-empty string", field="repository")

    events: list[EventInput] = []
    for raw in commits:
        if not isinstance(raw, dict):
            raise NormalizationError("each commit must be an object", field="commit")
        commit = raw.get("sha")
        if not isinstance(commit, str) or not commit.strip():
            # No sha means no scope: the record is not usable, and it is skipped, not invented.
            continue
        details = raw.get("commit")
        if not isinstance(details, dict) or not details:
            continue
        occurred_at = _parse_timestamp(_text(details.get("timestamp")))
        if occurred_at is None:
            author = raw.get("author")
            occurred_at = _parse_timestamp(author.get("date") if isinstance(author, dict) else None)
        if occurred_at is None:
            continue

        message = _text(details.get("message")) or commit
        message = message.replace("\r", " ").replace("\n", " ")
        verification = details.get("verification")

        events.append(
            EventInput(
                source_system_id=NYX_PRODUCER_SYSTEM_ID,
                event_type=EVENT_TYPE_COMMIT,
                occurred_at=occurred_at,
                correlation_id=commit,
                actor=_actor(_optional_dict(raw.get("committer"))),
                payload={
                    "repository": repository,
                    "message": message,
                    "distinct": bool(details.get("distinct")),
                    "verification": verification if isinstance(verification, dict) else None,
                },
                source_ref=_html_url(raw, repository, "commit", commit),
                trust_class=TrustClass.OBSERVED,
                labels=[],
                retention_class=RetentionClass.STANDARD,
                producer_event_id=provider_event_id("commit", repository, commit),
            )
        )
    return events


# --- required-field extraction ---------------------------------------------------------


def _require_head_sha(run: dict[str, Any]) -> str:
    """Return the run's head SHA. A run with no SHA is malformed: raise, never emit."""

    head = run.get("head_sha")
    if not isinstance(head, str) or not head.strip():
        raise NormalizationError(
            "run has no head_sha; a CI record with no commit cannot be scoped, so it is "
            "refused rather than emitted with an empty scope.commit",
            field="head_sha",
        )
    return head.strip()


def _require_name(run: dict[str, Any]) -> str:
    name = run.get("name")
    if not isinstance(name, str) or not name.strip():
        raise NormalizationError("run has no name", field="name")
    return name.strip()


#: GitHub's check-run vocabulary for "when this run finished": ``completed_at``, else
#: ``started_at``. A run with neither is rejected, never defaulted to the fetch time.
_CHECK_RUN_TIMESTAMP_FIELDS = ("completed_at", "started_at")
#: GitHub's Actions-run vocabulary. The Actions API reports ``run_started_at`` /
#: ``updated_at`` rather than the check-run names; the host's own timestamps, not a guess.
_ACTIONS_TIMESTAMP_FIELDS = ("run_started_at", "updated_at")


def _require_timestamp(run: dict[str, Any], repository_kind: str) -> datetime:
    """The run's own reported timestamp. A run with none raises.

    ``completed_at`` when present, else ``started_at`` (check runs) or ``run_started_at`` /
    ``updated_at`` (Actions runs). A missing timestamp is a rejected record, never a
    ``utc_now()`` guess: ``Event.occurred_at`` is required and NYX does not default it
    (``HANDOFF.md`` §8).
    """

    fields = (
        _CHECK_RUN_TIMESTAMP_FIELDS if repository_kind == "check" else _ACTIONS_TIMESTAMP_FIELDS
    )
    for field in fields:
        occurred_at = _parse_timestamp(run.get(field))
        if occurred_at is not None:
            return occurred_at
    raise NormalizationError(
        f"run has none of {', '.join(fields)}; a timestamp on a recorded event is "
        "required and is never defaulted to the fetch time",
        field=fields[0],
    )


def _validate_shape(run: dict[str, Any], repository: str) -> None:
    """Check the *required* shape of a run payload. Unknown keys are not inspected."""

    if not isinstance(run, dict):
        raise NormalizationError("run must be an object", field="run")
    if not isinstance(repository, str) or not repository.strip():
        raise NormalizationError("repository must be a non-empty string", field="repository")

    external_id = run.get("id")
    if isinstance(external_id, bool) or not isinstance(external_id, (str, int)):
        raise NormalizationError("run id must be a string or integer", field="id")
    if not str(external_id).strip():
        raise NormalizationError("run id must not be empty", field="id")

    conclusion = _conclusion(run)
    if conclusion is not None and conclusion not in _CONCLUSIONS:
        raise NormalizationError(
            "conclusion is not a word GitHub uses for a CI result", field="conclusion"
        )


def _validate_field_lengths(run: dict[str, Any]) -> None:
    """Reject values that would overflow the model they feed, before the model says so."""

    for field in ("name", "head_sha"):
        value = run.get(field)
        if isinstance(value, str) and len(value) > _MAX_TEXT:
            raise NormalizationError(f"{field} is longer than {_MAX_TEXT} characters", field=field)
    conclusion = _conclusion(run)
    if conclusion is not None and len(conclusion) > _MAX_CONCLUSION:
        raise NormalizationError(
            f"conclusion is longer than {_MAX_CONCLUSION} characters", field="conclusion"
        )


def _has_usable_observation(state: str, conclusion: str | None) -> bool:
    """Whether the run carries an observation worth recording.

    A ``completed`` run, or a run whose conclusion the host already reported, is usable. A
    ``queued`` / ``in_progress`` run with no conclusion is not: there is nothing to record, so
    the caller returns ``[]`` rather than emitting an empty event.
    """

    return state == _COMPLETED_STATUS or conclusion is not None


def _conclusion(run: dict[str, Any]) -> str | None:
    """The run's conclusion, lowercased, or ``None`` when the host reported none.

    GitHub reports ``null`` for a run that has not finished. ``None`` is not an error: the
    caller decides whether the run is usable.
    """

    value = run.get("conclusion")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise NormalizationError(
            "conclusion must be a non-empty string when present", field="conclusion"
        )
    return value.strip().lower()


def _state(run: dict[str, Any]) -> str:
    """The run's status (``queued`` / ``in_progress`` / ``completed``)."""

    value = run.get("status")
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise NormalizationError("status must be a non-empty string when present", field="status")
    return value.strip().lower()


def _suite(run: dict[str, Any]) -> str | None:
    """The check suite's display title, when the payload carries one."""

    suite = run.get("check_suite")
    if not isinstance(suite, dict):
        return None
    return _text(suite.get("display_title"))


def _head_branch(run: dict[str, Any]) -> str | None:
    """The branch the Actions run was started on, when the payload carries one."""

    return _text(run.get("head_branch"))


def _actor(run: dict[str, Any] | None) -> PrincipalRef | None:
    """The GitHub actor as a ``kind=tool`` principal. Omitted when the payload has no actor.

    Never invented: a payload with no actor produces ``None``, not a fabricated identity. The
    login is namespaced to the external provider so a GitHub user cannot collide with a NYX
    principal.
    """

    if not isinstance(run, dict):
        return None
    login = _text(run.get("login"))
    if login is None:
        return None
    return PrincipalRef(id=f"gh:{login}", kind=PrincipalKind.TOOL, display_name=login)


def _run_actor(run: dict[str, Any]) -> PrincipalRef | None:
    """Unwrap the actor object a run payload carries.

    Check runs name it ``actor``; Actions runs name it ``triggering_actor``. A run with
    neither has no actor, and ``None`` is returned rather than an identity being invented.
    """

    for key in ("actor", "triggering_actor"):
        raw = run.get(key)
        if isinstance(raw, dict):
            return _actor(raw)
    return None


def _producer_identity() -> PrincipalRef:
    """The producer identity recorded on the evidence: the adapter itself, as a tool."""

    return PrincipalRef(
        id=NYX_PRODUCER_SYSTEM_ID,
        kind=PrincipalKind.TOOL,
        display_name="NYX git-ci adapter",
    )


def _text(value: Any) -> str | None:
    """Return a trimmed non-empty string, or ``None`` for anything else."""

    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _optional_dict(value: Any) -> dict[str, Any] | None:
    """Return ``value`` when it is a mapping, else ``None`` -- never a fabricated object."""

    return value if isinstance(value, dict) else None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, rejecting a naive one. ``None`` for absent/invalid."""

    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


def _html_url(run: dict[str, Any], repository: str, kind: str, external_id: Any) -> str:
    """The run's ``html_url`` when the host reported one, else a deterministic GitHub URL."""

    reported = _text(run.get("html_url"))
    if reported is not None:
        return reported
    if isinstance(external_id, str) and external_id:
        return f"https://github.com/{repository}/{kind}/{external_id}"
    return f"https://github.com/{repository}/{kind}"


# --- evidence text ---------------------------------------------------------------------


def _workflow_title(name: str, branch: str | None) -> str:
    return f"{name} ({branch})" if branch else name


def _evidence_description(name: str, conclusion: str | None, suite: str | None, state: str) -> str:
    """State the conclusion in GitHub's own words, plus the suite. Nothing is invented.

    C-001 reads the outcome from the title and description with ``\\bpass(?:es|ed|ing)?\\b`` and
    ``\\bfail(?:s|ed|ing|ure)?\\b``, so a ``success`` conclusion must appear as the bare word
    ``success`` and a ``failure`` as ``failure``. A ``neutral``/``skipped``/``cancelled``
    conclusion is preserved verbatim and is deliberately *not* turned into a pass or a fail.
    """

    parts = [f"GitHub check run '{name}'"]
    if suite:
        parts.append(f" (suite: {suite})")
    if conclusion:
        parts.append(f" concluded {conclusion}")
    elif state:
        parts.append(f" reported status {state}")
    else:
        parts.append(" reported no conclusion")
    return "".join(parts) + "."


def _workflow_description(name: str, conclusion: str | None, branch: str | None, state: str) -> str:
    """The Actions-run counterpart of :func:`_evidence_description`.

    An Actions run reports a workflow conclusion, not a per-job test result, so the branch is
    stated alongside it. No pass/fail word is added that the host did not use.
    """

    parts = [f"GitHub Actions workflow '{name}'"]
    if branch:
        parts.append(f" on branch {branch}")
    if conclusion:
        parts.append(f" concluded {conclusion}")
    elif state:
        parts.append(f" reported status {state}")
    else:
        parts.append(" reported no conclusion")
    return "".join(parts) + "."


def _limitations(conclusion: str | None) -> list[str]:
    limitations = [DEFAULT_LIMITATION]
    if conclusion not in ("success", "failure"):
        limitations.append(NO_OUTCOME_LIMITATION)
    return limitations


__all__ = [
    "ACCESS_POLICY_REF",
    "DEFAULT_LIMITATION",
    "EVENT_TYPE_CHECK",
    "EVENT_TYPE_COMMIT",
    "EVENT_TYPE_WORKFLOW",
    "NYX_PRODUCER_SYSTEM_ID",
    "NormalizationError",
    "normalize_actions_run",
    "normalize_check_run",
    "normalize_commits",
    "provider_event_id",
]
