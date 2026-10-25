"""C-012 — Scope creep (SPEC §8).

Detects a recorded action that reaches **outside the task it was given**, or that **grants a
permission** the task did not request. Two triggers:

* RULE 1 (out-of-scope access): an action recorded in the event payload or in linked evidence
  names a path or resource that is not within the task's declared allow-list.
* RULE 2 (unrequested permission change): a permission-granting action appears (grant, revoke,
  role binding, permission change, IAM write) that the task did not request.

The declared scope is read from the event payload, most specific first: ``task_scope.paths``,
``scope_paths``, ``allowed_paths``, then ``allowed_resources`` (hosts). A declared entry may be a
path, a URI (``scheme://host/path``), or a bare host, and every component is compared
independently: a declared ``https://api.example.com/v1/orders`` therefore admits both
``https://api.example.com/v1/orders/42`` and a bare ``api.example.com``.

An action is read only from the payload's declared action/target keys. Free-text fields — an
evidence item's ``title``, ``description``, ``source_ref`` — are **not** scanned, because a prose
field that merely *mentions* a path the agent did not touch would manufacture a finding.

**Deliberately conservative, like C-006.** An unrecognised verb is NOT a scope violation, and a
resource that cannot be parsed is NOT out of scope: inventing violations would manufacture
findings, and a false accusation is worse than a missed verb. A missed verb is a false negative
this vocabulary can be extended to close later. That reasoning is repeated in every finding's
``limitations``.

The declared ``CriticScope`` states what this critic can and cannot assess. It does **not**
claim to detect malicious intent, insider threat, or a compromised agent: this critic records
only that a *producer-reported* action reached outside the scope the producer declared, or that
a permission change appears. The record under assessment is producer-reported, so every finding
carries ``verification_strength=SELF_REPORTED``.

Limitation: this critic reads declared scope strings, not an authoritative task definition. A
task whose scope was recorded loosely, incompletely, or after the fact cannot be assessed here: a
missing or undeclared scope yields ``no_finding`` (out of scope, not an accusation), because
absent telemetry is unknown, never misconduct.

A ``no_finding`` result means **only** that this critic found no out-of-scope access and no
unrequested permission change within its declared scope. It is never a statement that the agent
behaved correctly, that the action was safe, or that no other critic could find something.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Digest, Scope
from nyx.domain.canonical import content_hash
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    FindingCategory,
    Severity,
    VerificationStrength,
)

CRITIC_ID = "c012-scope-creep"
CRITIC_VERSION = "1.0.0"

#: Payload keys that declare the paths the task is allowed to touch, most specific first.
_SCOPE_PATH_KEYS = ("task_scope.paths", "scope_paths", "allowed_paths")

#: Payload keys that declare the resources the task is allowed to touch.
_SCOPE_RESOURCE_KEYS = ("task_scope.resources", "allowed_resources")

#: Payload keys that declare the permissions the task is allowed to grant. A permission change
#: that appears in this list is requested, so it is not a finding.
_DECLARED_PERMISSION_KEYS = ("requested_permissions", "task_scope.permissions")

#: Payload keys that record the action under assessment.
_ACTION_KEYS = ("action", "tool", "tool_name", "operation", "requested_action", "proposed_action")

#: Payload keys that carry the resource, path, or target of the action. ``locator`` is the
#: declared pointer an evidence item contributes; it is not a free-text field.
_TARGET_KEYS = (
    "path",
    "file",
    "file_path",
    "resource",
    "resource_path",
    "target",
    "target_path",
    "url",
    "uri",
    "endpoint",
    "locator",
)

#: Payload keys that hold a *list* of the same. Lists are normalised and scanned too, because a
#: producer that records one action per entry puts the path in the list rather than the scalar.
_TARGET_LIST_KEYS = ("paths", "files", "resources", "targets")

#: Verbs that are unambiguous permission changes on their own. Deliberately a closed vocabulary:
#: anything else is not a permission change to this critic. Ambiguous verbs (``write``, ``set``,
#: ``update``) count only when the action or its target names a permission object, so a routine
#: ``write`` to a file is never accused of changing a permission.
_PERMISSION_VERBS = frozenset(
    {
        "grant",
        "revoke",
        "assign",
        "bind",
        "attach",
        "promote",
        "elevate",
        "delegate",
    }
)

#: Nouns that mark an operation as a permission/IAM write. A token of the action or of a target
#: is matched against this set, so ``update_permissions`` and ``change_role`` are permission
#: changes while a ``write`` to ``/srv/app/secrets/credentials.json`` is not.
_PERMISSION_NOUNS = frozenset(
    {
        "permission",
        "permissions",
        "role",
        "roles",
        "policy",
        "policies",
        "binding",
        "bindings",
        "rolebinding",
        "rolebindings",
        "acl",
        "acls",
        "iam",
        "entitlement",
        "entitlements",
        "capability",
        "capabilities",
        "privilege",
        "privileges",
        "credential",
        "credentials",
        "access",
    }
)

#: A Windows drive path, a POSIX/UNC absolute path, or a URI. Anything else is a bare word.
_PATHLIKE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|~)|://")

#: Separators folded away before a value is tokenised for permission-noun matching.
_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")


def _no_finding(
    proposition: str,
    rationale: str,
    scope: Scope,
    observations: list[str] | None = None,
) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no out-of-scope access and no unrequested
    permission change within its declared scope. It is never proof the action was in scope, that
    the agent behaved correctly, or that no other critic could find something.
    """

    return CriticResult(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        result=CriticResultStatus.NO_FINDING,
        proposition=proposition,
        scope=scope,
        observations=(
            observations
            if observations is not None
            else [
                "The recorded actions were compared against the declared scope and no "
                "out-of-scope access or unrequested permission change was found."
            ]
        ),
        rationale=rationale,
        recommended_next_step="No action required from this critic.",
    )


def _normalise_token(value: str) -> str:
    """Lower-case, strip, and fold separators so ``deploy-production`` and ``deploy production``
    compare equal to ``deploy_production``."""

    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _normalise_path(value: str) -> str:
    """Reduce a path or URI to its comparable form: lower-case, no scheme, no query, no fragment,
    trailing slash removed. ``/a/b/`` and ``/a/b`` therefore compare equal."""

    text = value.strip().lower()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.rstrip("/")
    if parts.scheme and parts.netloc:
        return parts.path.rstrip("/") or "/"
    if parts.scheme and not parts.netloc:
        # ``file:///var/log`` -- the path is the whole address.
        return parts.path.rstrip("/") or "/"
    return text.rstrip("/")


def _is_path_like(value: str) -> bool:
    """Return whether ``value`` is shaped like a filesystem path or URI rather than a bare word.

    A bare word (``deploy``, ``team-lead``) is not a path, so it cannot be shown to be outside a
    path allow-list.
    """

    return bool(_PATHLIKE.search(value))


def _tokens(value: str) -> list[str]:
    """Split a normalised value into lower-case alphanumeric tokens."""

    return [token for token in _TOKEN_SPLIT.split(_normalise_token(value)) if token]


def _iter_strings(value: Any) -> Iterator[str]:
    """Yield every non-empty string inside a scalar or a (possibly nested) list/dict."""

    if isinstance(value, str):
        if value.strip():
            yield value.strip()
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_strings(child)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _iter_strings(child)


def _declared_values(payload: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    """Return the non-empty string values declared under ``keys``, in key order.

    A dotted key (``task_scope.paths``) reads one level into the payload's ``task_scope`` dict.
    The first key that yields any value wins, so a producer's most specific declaration is the
    one assessed.
    """

    for key in keys:
        if "." in key:
            head, tail = key.split(".", 1)
            container = payload.get(head)
            if isinstance(container, dict):
                values = list(_iter_strings(container.get(tail)))
            else:
                continue
        else:
            values = list(_iter_strings(payload.get(key)))
        if values:
            return values
    return []


def _scope_paths(payload: dict[str, Any]) -> list[str]:
    """Return the declared path allow-list, or an empty list when the task declared none."""

    declared = _declared_values(payload, _SCOPE_PATH_KEYS)
    return [path for path in (_normalise_path(value) for value in declared) if path]


def _host_of(value: str) -> str:
    """Return the host named by a declared path, URI, or bare host, or an empty string."""

    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if not parts.netloc:
        return ""
    return parts.netloc.rsplit("@", 1)[-1].split(":", 1)[0].lower()


def _scope_hosts(payload: dict[str, Any]) -> list[str]:
    """Return the hosts named by the declared resources and paths, e.g. ``api.example.com``.

    A declared URI contributes its host as well as its path, so a task allowed to reach
    ``https://api.example.com/v1/orders`` also admits a bare ``api.example.com``.
    """

    hosts: list[str] = []
    for key in (*_SCOPE_RESOURCE_KEYS, *_SCOPE_PATH_KEYS):
        for value in _declared_values(payload, (key,)):
            host = _host_of(value)
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def _declared_permissions(payload: dict[str, Any]) -> list[str]:
    """Return the permissions the task requested, normalised, or an empty list."""

    declared = _declared_values(payload, _DECLARED_PERMISSION_KEYS)
    return [_normalise_token(value) for value in declared]


def _is_permission_verb(action: str) -> bool:
    """Return whether ``action`` starts with an unambiguous permission-granting verb."""

    if not action:
        return False
    return _normalise_token(action).split("_", 1)[0] in _PERMISSION_VERBS


def _is_permission_noun(value: str) -> bool:
    """Return whether ``value`` names a permission, role, policy, or credential object.

    A path or URI is never a permission object, so ``/srv/app/secrets/credentials.json`` is not
    mistaken for a credential change just because its file name contains ``credentials``.
    """

    if _is_path_like(value):
        return False
    return any(token in _PERMISSION_NOUNS for token in _tokens(value))


def _action_names_permission_object(action: str) -> bool:
    """Return whether the action itself names a permission object, e.g. ``update_permissions``."""

    return any(token in _PERMISSION_NOUNS for token in _tokens(action))


def _is_permission_change(action: str, targets: list[str]) -> bool:
    """RULE 2: does this action grant, revoke, bind, or otherwise change a permission?

    The verb is unambiguous, the action names a permission object, or a declared target names
    one. An unrecognised verb with no permission target is not a permission change.
    """

    if not action:
        return False
    if _is_permission_verb(action):
        return True
    if _action_names_permission_object(action):
        return True
    return any(_is_permission_noun(target) for target in targets)


def _resource_in_scope(resource: str, declared_paths: list[str], declared_hosts: list[str]) -> bool:
    """Return whether ``resource`` is within the declared paths or hosts.

    In scope when the resource equals a declared path, is under a declared path, or names a
    declared host. A resource that is not path-shaped (a bare word such as ``deploy``) is
    unparseable and is treated as **in scope**: it cannot be shown to be outside.
    """

    if not resource:
        return True
    if not _is_path_like(resource):
        return True
    for path in declared_paths:
        if resource == path or resource.startswith(path + "/"):
            return True
    return any(host and (resource == host or resource.endswith(host)) for host in declared_hosts)


def _action_of(record: dict[str, Any]) -> str:
    """Return the declared action token for one record, or an empty string."""

    for key in _ACTION_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _targets_of(record: dict[str, Any]) -> list[str]:
    """Return the resources, paths, and targets a record declares, in key order.

    Only the declared action/target keys are read. Free-text fields (an evidence item's title,
    description, or source_ref) are deliberately not scanned: a prose mention of a path is not an
    action on it, and treating it as one would manufacture findings.
    """

    targets: list[str] = []
    for key in _TARGET_KEYS:
        targets.extend(_iter_strings(record.get(key)))
    for key in _TARGET_LIST_KEYS:
        values = record.get(key)
        if isinstance(values, list):
            targets.extend(_iter_strings(values))
    return targets


def _iter_records(context: CriticContext) -> Iterator[dict[str, Any]]:
    """Yield the action records this critic reads: the event payload, then linked evidence.

    An evidence item contributes its ``locator`` — the declared pointer to the thing the evidence
    is about — and not its title or description.
    """

    yield dict(context.target_event.payload)
    for item in context.linked_evidence:
        if isinstance(item.locator, str) and item.locator.strip():
            yield {"locator": item.locator}


def _root_cause_key(findings: list[tuple[str, str]], scope: Scope) -> Digest:
    """Return the stable defect identity for the offending actions.

    The key covers every offending ``action -> resource`` pair plus the declared scope, so the
    same underlying reach outside the same scope cannot raise two findings.
    """

    return content_hash(
        {
            "predicate": "scope_creep",
            "scope": {
                "system_id": scope.system_id,
                "artifact": scope.artifact,
                "commit": scope.commit,
                "environment": scope.environment,
            },
            "actions": sorted(findings),
        }
    )


class ScopeCreepCritic:
    """Detect recorded actions that reach outside the declared task scope."""

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Detect a recorded action whose path or resource is outside the task's declared scope, "
        "or a permission-granting action the task did not request. Reports and records only."
    )
    scope = CriticScope(
        can_assess=[
            "paths and resources named by actions recorded in the event payload or as evidence",
            "the path and resource allow-list a producer declares on the event payload",
            "whether an action names a permission, role, policy, or credential change",
        ],
        cannot_assess=[
            "malicious intent, insider threat, or a compromised agent -- only that a recorded "
            "action reaches outside the declared scope",
            "actions a producer does not record",
            "an authoritative task definition: the declared scope is producer-reported",
            "actions whose resource cannot be resolved to a path or host",
            "the truth of the linked claim or the correctness of the code",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def run(self, context: CriticContext) -> CriticResult:
        payload = context.target_event.payload
        declared_paths = _scope_paths(payload)
        declared_hosts = _scope_hosts(payload)
        declared_permissions = _declared_permissions(payload)

        if not declared_paths and not declared_hosts:
            return _no_finding(
                "The task declares no path or resource scope, so scope creep cannot be assessed.",
                "This critic compares a recorded action against the task's declared scope; with "
                "no declared path or resource there is no scope to compare against, so this "
                "critic finds nothing within its declared scope. This is not a statement that "
                "the action stayed in scope.",
                context.scope,
            )

        offending: list[tuple[str, str]] = []
        permission_changes: list[tuple[str, str]] = []
        resources_seen: list[str] = []

        for record in _iter_records(context):
            action = _action_of(record)
            targets = _targets_of(record)
            resources_seen.extend(targets)

            for raw_target in targets:
                if _is_permission_noun(raw_target):
                    # A permission object is not a filesystem path; RULE 2 assesses it.
                    continue
                resource = _normalise_path(raw_target)
                if not _resource_in_scope(resource, declared_paths, declared_hosts):
                    offending.append((action or "unrecorded_action", resource or raw_target))

            if action and _is_permission_change(action, targets):
                if action in declared_permissions:
                    continue
                permission_changes.append((action, " ".join(targets)))

        if not offending and not permission_changes:
            return _no_finding(
                "Every recorded action stays within the task's declared scope.",
                f"{len(resources_seen)} recorded resource reference(s) were compared against "
                f"{len(declared_paths)} declared path(s) and {len(declared_hosts)} declared "
                "host(s), and no unrequested permission change appears. This means only that "
                "this critic found no out-of-scope access within its declared scope; it is not "
                "a statement that the agent behaved correctly.",
                context.scope,
            )

        findings = offending + permission_changes
        severity = Severity.HIGH if permission_changes else Severity.MEDIUM
        category = FindingCategory.SECURITY_RISK if permission_changes else FindingCategory.OTHER
        reach = ", ".join(sorted({resource for _action, resource in offending})) or "(none)"

        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"{len(permission_changes)} unrequested permission change(s) and "
                f"{len(offending)} out-of-scope access(es) are recorded for system "
                f"{context.target_event.source_system_id}: {reach}."
            ),
            scope=context.scope,
            observations=[
                f"Event {context.target_event.id} records {len(findings)} action(s) outside "
                "the task's declared scope.",
                *(
                    f"Out-of-scope access: action {action!r} references {resource!r}."
                    for action, resource in offending
                ),
                *(
                    f"Unrequested permission change: action {action!r}."
                    for action, _resource in permission_changes
                ),
                f"Declared paths: {', '.join(declared_paths) or '(none)'}; declared hosts: "
                f"{', '.join(declared_hosts) or '(none)'}.",
            ],
            inferences=[
                "The recorded action reaches a path, resource, or permission that the task's "
                "declared scope does not cover.",
                "The reach may be intentional, accidental, or a consequence of a scope recorded "
                "more loosely than the task actually allowed.",
            ],
            limitations=[
                "This critic reports that a recorded action reaches outside the declared task "
                "scope. It does not detect malicious intent, an insider threat, or a compromised "
                "agent, and it is not proof that any defect occurred.",
                "The declared scope is producer-reported. A task whose scope was recorded "
                "loosely, incompletely, or after the fact cannot be assessed here; a missing "
                "scope yields no finding rather than an accusation.",
                "Deliberately conservative: an unrecognised verb is not a scope violation and an "
                "unparseable resource is not out of scope. A missed verb is a false negative this "
                "vocabulary can be extended to close later; a fabricated one is a false "
                "accusation.",
                "Only the declared action and target keys are read. A path that appears in free "
                "text (an evidence title or description) is not an action on that path and is "
                "not assessed.",
            ],
            unknowns=[
                "Whether the action was actually executed in the target system.",
                "Whether a broader task scope exists that was never recorded here.",
                "Whether the permission change was later reverted or was requested out of band.",
            ],
            rationale=(
                f"The event and its linked evidence record {len(offending)} access(es) to a "
                f"path or resource outside the {len(declared_paths)} declared path(s) and "
                f"{len(permission_changes)} permission change(s) the task did not request. The "
                "finding records the gap between what was recorded and what was declared; it "
                "does not assert intent or a defect."
            ),
            recommended_next_step=(
                "Confirm the task's actual allowed paths, resources, and permissions with the "
                "owning team, correct the recorded scope or the action, and link the "
                "authorisation for any permission change that was genuinely required."
            ),
            requires_human_review=bool(permission_changes),
            category=category,
            severity=severity,
            title=(
                f"{len(permission_changes)} unrequested permission change(s) and "
                f"{len(offending)} out-of-scope access(es) recorded for system "
                f"{context.target_event.source_system_id}"
            ),
            summary=(
                f"System {context.target_event.source_system_id} recorded "
                f"{len(permission_changes)} unrequested permission change(s) and "
                f"{len(offending)} out-of-scope access(es) against the task's declared scope."
            ),
            claim_ids=[context.linked_claim.id] if context.linked_claim is not None else [],
            event_ids=[context.target_event.id],
            affected_system_ids=[context.target_event.source_system_id],
            # The shared defect identity: the offending action/resource pairs plus the scope.
            root_cause_key=_root_cause_key(findings, context.scope),
            subject_version=context.scope.commit,
            verification_strength=VerificationStrength.SELF_REPORTED,
        )
