"""One normalisation for "the same fact about the same subject", shared by C-002 and C-009.

C-002 compares **claims** and C-009 compares **evidence**, and the brief asks for a boundary
between them. The boundary is already right in the code: C-002 reads claim statements for the same
subject, C-009 reads run outcomes for the same defined test. What they lacked was a shared
*identity* for the underlying defect, so one conflict could raise two findings.

This module provides that identity and nothing else. It does not merge the critics, does not
change their scope, and does not make either abstain. It gives both a key to record.

The rule for the key is deliberately boring:

* the **fact** is what the records disagree *about* -- a normalised predicate plus its subject;
* the **subject** is the thing the fact is about -- a claim's ``scope``, an evidence item's
  declared scope;
* the **root cause** is the pair. Two records that disagree about the same fact on the same
  subject are the same underlying conflict, whether they arrived as claims or as evidence.

A key that tried to be cleverer than that -- one that normalised prose, or that guessed at intent
-- would merge incidents that merely sound alike, which is the failure mode the existing dedupe
docstring already forbids.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from pydantic import StringConstraints

from nyx.domain.base import Digest, Scope
from nyx.domain.canonical import content_hash

Fact = Annotated[str, StringConstraints(min_length=1, max_length=256)]


@dataclass(frozen=True, slots=True)
class FactKey:
    """The identity of one fact about one subject.

    ``predicate`` is a normalised name for what is being asserted ("outcome", "completion",
    "status"), not the text of the assertion. ``subject`` identifies what it is about. Together
    they are the ``root_cause_key`` recorded on a finding.
    """

    predicate: Fact
    subject: str

    def to_key(self) -> Digest:
        """Return the stable digest used as a finding's ``root_cause_key``."""

        return content_hash({"predicate": self.predicate, "subject": self.subject})


def _normalise_scope(scope: Scope | None) -> str | None:
    """Render a scope as the subject half of a fact key.

    Only fields that are definitely declared participate. A scope that declares no commit is not
    an unscoped version of one that does -- it is a different, weaker statement, and treating the
    two as the same subject would merge a versioned conflict with an unversioned one.
    """

    if scope is None:
        return None
    parts = [scope.system_id]
    if scope.artifact:
        parts.append(f"artifact={scope.artifact}")
    if scope.commit:
        parts.append(f"commit={scope.commit}")
    if scope.environment:
        parts.append(f"env={scope.environment}")
    return "|".join(parts)


def subject_key(scope: Scope | None, fallback: str | None = None) -> str:
    """Return the subject identity for a scope, or ``fallback`` when the scope declares nothing.

    ``fallback`` exists because a claim with a bare system scope still needs *some* subject to be
    comparable; the caller supplies the artifact name it actually has. A scope that declares
    nothing and has no fallback yields an empty string, and callers must treat an empty subject as
    "not comparable" rather than as a match.
    """

    rendered = _normalise_scope(scope)
    if rendered:
        return rendered
    return fallback or ""


def outcome_fact_key(
    *,
    predicate: Fact,
    scope: Scope | None,
    fallback_subject: str | None = None,
) -> FactKey:
    """Build the fact key for two records that disagree about an **outcome**.

    Used by both C-002 and C-009: C-002 for two claims whose stances disagree, C-009 for a passing
    and a failing run. Same predicate, same subject, same key -- which is exactly what makes one
    conflict produce one root cause rather than two findings.
    """

    return FactKey(predicate=predicate, subject=subject_key(scope, fallback_subject))


def same_subject(left: Scope | None, right: Scope | None) -> bool:
    """Return whether two scopes identify the same subject.

    Both sides must declare the same values for every field that *either* declares. A missing
    field is not a wildcard: an evidence item that does not name a commit is not comparable with
    one that does, so it cannot be the same subject as anything.
    """

    if left is None or right is None:
        return False
    for field_name in ("artifact", "commit", "environment"):
        left_value = getattr(left, field_name)
        right_value = getattr(right, field_name)
        if left_value is None and right_value is None:
            continue
        if left_value != right_value:
            return False
    return left.system_id == right.system_id


def group_records[T](records: list[T], scope_of: Callable[[T], Scope | None]) -> dict[str, list[T]]:
    """Group records by subject identity, for the "same fact, same subject" comparison.

    ``scope_of`` is injected so this module holds no domain imports and works for both claims and
    evidence. Records whose scope declares nothing land under the empty key, and the caller must
    not compare across that key.
    """

    groups: dict[str, list[T]] = {}
    for record in records:
        scope = scope_of(record)
        rendered = _normalise_scope(scope)
        groups.setdefault(rendered or "", []).append(record)
    return groups
