"""C-012 — Scope creep (SPEC §8).

Detects a recorded action that reaches **outside the task it was given**, or that **grants a
permission** the task did not request. One rule, two triggers:

* RULE 1 (out-of-scope access): an action recorded in the event payload or in linked evidence
  names a path or resource that is not within the task's declared scope.
* RULE 2 (unrequested permission change): a permission-granting action appears (grant, revoke,
  role binding, permission change, IAM write) that the task did not ask for.

The declared scope is read from the event payload, in descending order of specificity:
``task_scope`` (``{"paths": [...], "resources": [...]}``), ``scope_paths``,
``allowed_paths``, then ``allowed_resources``. Each entry may be a path, a URI
(``scheme://host/path``), or a bare host: every component is compared independently, so a
declared ``https://api.example.com/v1/orders`` admits both ``https://api.example.com/v1/orders/42``
and a bare ``api.example.com``. An action whose resource cannot be resolved to a non-empty
path/host is skipped rather than judged.

**Deliberately conservative, like C-006.** An unrecognised verb is NOT a scope violation, and a
resource that cannot be parsed is NOT out of scope: inventing violations would manufacture
findings, and a false accusation is worse than a missed verb. A missed verb is a false negative
this vocabulary can be extended to close later. That reasoning is repeated in every finding's
``limitations``.

The declared ``CriticScope`` states what this critic can and cannot assess. It does **not**
claim to detect malicious intent, insider threat, or a compromised agent: this critic records
only that a *producer-reported* action reached outside the scope the producer declared, or
that a permission change appears. The record under assessment is producer-reported, so every
finding carries ``verification_strength=SELF_REPORTED``.

Limitation: this critic reads declared scope strings, not an authoritative task definition. A
task whose scope was recorded loosely, incompletely, or after the fact cannot be assessed here:
a missing or undeclared scope yields ``no_finding`` (out of scope, not an accusation), because
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

#: Payload keys that carry the resource, path, or target of the action.
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
)

#: Payload keys that hold a *list* of the same. Lists are normalised and scanned too, because a
#: producer that records one action per entry will put the path in the list rather than the scalar.
_TARGET_LIST_KEYS = ("paths", "files", "resources", "targets")

#: Permission-granting verbs. Deliberately a closed vocabulary: anything else is not a permission
#: change to this critic. ``grant_admin`` and ``change_permissions`` both normalise to a token
#: whose head is in this set.
_PERMISSION_VERBS = frozenset(
    {
        "grant",
        "revoke",
        "assign",
        "bind",
        "attach",
        "set",
        "add",
        "remove",
        "create",
        "update",
        "change",
        "modify",
        "write",
        "enable",
        "disable",
        "promote",
        "elevate",
        "delegate",
        "rotate_credentials",
    }
)

#: Nouns that mark an operation as a permission/IAM write. The head token of an action is matched
#: against the head of each noun, so ``modify_permissions`` and ``update_permissions`` are the
#: same permission change.
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

#: Markers of a scope restriction that makes an action out-of-scope even when the resource itself
#: was never declared. Only explicit restriction markers count: a bare resource name is not a
#: restriction, so this cannot be used to manufacture a finding from an undeclared scope.
_EXPLICIT_RESTRICTION = re.compile(
    r"\b(out[_\s-]?of[_\s-]?scope|unauthori[sz]ed|unauthorized|forbidden|not[_\s-]?allowed"
    r"|outside[_\s-]?scope|disallowed|prohibited|denied)\b"
)

#: Markers of a permission change in prose. Explicit only; a plain "set" is not a permission.
_PERMISSION_PROSE = re.compile(
    r"\b(grant|revoke|assign|attach|bind|change|update|modify|add|remove)\w*\s+"
    r"(permission|permissions|role|roles|policy|policies|binding|bindings|acl|iam|"
    r"capability|capabilities|privilege|privileges|credential|credentials|entitlement)s?\b"
    r"|\b(iam|permission|role|policy|acl|rbac)\s+(write|change|update|modify|binding)s?\b"
    r"|\bpermission\s+(change|grant|escalation)s?\b"
    r"|\brole\s+bindings?\b"
)


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
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
        observations=[],
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
        parts = None
    if parts is not None and parts.scheme and parts.netloc:
        return parts.path.rstrip("/") or "/"
    if parts is not None and parts.scheme and not parts.netloc:
        # ``file:///var/log`` -- the path is the whole address.
        return parts.path.rstrip("/") or "/"
    return text.rstrip("/")


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


def _scope_paths(payload: dict[str, Any]) -> list[str]:
    """Return the declared path allow-list, or an empty list when the task declared none."""

    for key in _SCOPE_PATH_KEYS:
        if key == "task_scope.paths":
            task_scope = payload.get("task_scope")
            if isinstance(task_scope, dict):
                values = _iter_strings(task_scope.get("paths"))
            else:
                continue
        else:
            values = _iter_strings(payload.get(key))
        paths = [path for path in (_normalise_path(value) for value in values) if path]
        if paths:
            return paths
    return []


def _scope_hosts(payload: dict[str, Any]) -> list[str]:
    """Return the hosts named by the declared resources, e.g. ``api.example.com``."""

    hosts: list[str] = []
    for key in _SCOPE_RESOURCE_KEYS:
        if key == "task_scope.resources":
            task_scope = payload.get("task_scope")
            if isinstance(task_scope, dict):
                values = _iter_strings(task_scope.get("resources"))
            else:
                continue
        else:
            values = _iter_strings(payload.get(key))
        for value in values:
            try:
                parts = urlsplit(value)
            except ValueError:
                parts = None
            if parts is not None and parts.netloc:
                host = parts.netloc.rsplit("@", 1)[-1].split(":", 1)[0]
                if host:
                    hosts.append(host.lower())
    return hosts


def _declared_permissions(payload: dict[str, Any]) -> list[str]:
    """Return the permissions the task requested, normalised, or an empty list."""

    tokens: list[str] = []
    for key in _DECLARED_PERMISSION_KEYS:
        if key == "task_scope.permissions":
            task_scope = payload.get("task_scope")
            if isinstance(task_scope, dict):
                values = _iter_strings(task_scope.get("permissions"))
            else:
                continue
        else:
            values = _iter_strings(payload.get(key))
        tokens.extend(_normalise_token(value) for value in values)
    return tokens


def _is_permission_verb(action: str) -> bool:
    """Return whether ``action`` starts with a permission-granting verb."""

    if not action:
        return False
    return _normalise_token(action).split("_", 1)[0] in _PERMISSION_VERBS


def _is_permission_noun(action: str) -> bool:
    """Return whether ``action`` names a permission, role, policy, or credential object."""

    for noun in _PERMISSION_NOUNS:
        if _normalise_token(action).split("_", 1)[0] == _normalise_token(noun):
            return True
    return False


def _is_permission_change(action: str, target: str) -> bool:
    """RULE 2: does this action grant, revoke, bind, or otherwise change a permission?

    Either the verb is a permission verb, or the target (or the action text itself, for prose
    records) carries an explicit permission marker. An unrecognised verb with no permission
    marker is not a permission change.
    """

    if not action:
        return False
    if _is_permission_verb(action):
        return True
    if _is_permission_noun(target):
        return True
    return bool(_PERMISSION_PROSE.search(f"{action} {target}".lower()))


def _resource_in_scope(resource: str, declared_paths: list[str], declared_hosts: list[str]) -> bool:
    """Return whether ``resource`` is within the declared paths or hosts.

    In scope when the resource equals a declared path, is under a declared path, or names a
    declared host. A resource with no path and no host (a bare word such as ``deploy``) is
    unparseable and is treated as **in scope**: it cannot be shown to be outside.
    """

    if not resource:
        return True
    for path in declared_paths:
        if resource == path or resource.startswith(path + "/"):
            return True
    for host in declared_hosts:
        if host and (resource == host or resource.endswith(host)):
            return True
    return False


def _iter_targets(record: dict[str, Any]) -> Iterator[str]:
    """Yield every resource, path, or target named in one record's payload."""

    for key in _TARGET_KEYS:
        yield from _iter_strings(record.get(key))
    for key in _TARGET_LIST_KEYS:
        values = record.get(key)
        if isinstance(values, list):
            yield from _iter_strings(values)


def _iter_records(context: CriticContext) -> Iterator[dict[str, Any]]:
    """Yield the action records this critic reads: the event payload, then linked evidence."""

    yield dict(context.target_event.payload)
    for item in context.linked_evidence:
        if isinstance(item.source_ref, str):
            record = dict(item.model_dump())
            record.setdefault("source_ref", item.source_ref)
            yield record


def _root_cause_key(records: list[tuple[str, str]], scope: Scope) -> Digest:
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
            "actions": sorted(records),
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
        sources: list[str] = []
        resources_seen: list[str] = []

        for record in _iter_records(context):
            action = ""
            for key in _ACTION_KEYS:
                value = record.get(key)
                if isinstance(value, str) and value.strip():
                    action = value.strip().lower()
                    break

            for raw_target in _iter_targets(record):
                if _EXPLICIT_RESTRICTION.search(raw_target.lower()):
                    continue
                resource = _normalise_path(raw_target)
                resources_seen.append(resource or raw_target)
                if not _resource_in_scope(resource, declared_paths, declared_hosts):
                    offending.append((action or "unrecorded_action", resource or raw_target))

            if action and _is_permission_change(action, " ".join(resources_seen)):
                if action in declared_permissions:
                    continue
                permission_changes.append((action, " ".join(resources_seen)))

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
        title = (
            f"{len(permission_changes)} unrequested permission change(s) and "
            f"{len(offending)} out-of-scope access(es) recorded for system "
            f"{context.target_event.source_system_id}"
        )
        severity = Severity.HIGH if permission_changes else Severity.MEDIUM
        category = FindingCategory.SECURITY_RISK if permission_changes else FindingCategory.OTHER

        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"{len(permission_changes)} unrequested permission change(s) and "
                f"{len(offending)} out-of-scope access(es) are recorded for system "
                f"{context.target_event.source_system_id}."
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
                "Records that carry an explicit out-of-scope/unauthorised marker are excluded "
                "from matching, so this rule cannot fire on a record that already disclaims "
                "itself.",
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
            title=title,
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
