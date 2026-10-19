"""Identity-based finding deduplication (SPEC §11.2).

Two findings are the same finding only when they come from the same critic, about the same
target (claim or event identity), and within the same target scope. The key is a digest of
that identity tuple — never of the prose. Merging on text similarity is forbidden: two
incidents can sound alike and be unrelated, and one incident can be described in two ways.

When a duplicate is detected the new finding is still persisted, with ``status=duplicate``,
a link to the original, and the dedupe rationale recorded. History is appended to, never
overwritten (SPEC §3.2 invariant 5).
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy.orm import Session

from nyx.domain.base import Scope
from nyx.domain.canonical import content_hash
from nyx.domain.enums import FindingStatus
from nyx.findings.models import Finding
from nyx.storage.repositories.findings import FindingRepository

TargetType = Literal["claim", "event"]


def dedupe_key(
    *,
    critic_id: str,
    target_type: TargetType,
    target_id: str,
    scope: Scope,
) -> str:
    """Return the stable dedupe key for one (critic, target, scope) triple."""

    return content_hash(
        {
            "critic_id": critic_id,
            "target_type": target_type,
            "target_id": target_id,
            "system_id": scope.system_id,
            "artifact": scope.artifact,
            "commit": scope.commit,
            "environment": scope.environment,
            "period": None
            if scope.period is None
            else {
                "from": scope.period.from_,
                "to": scope.period.to,
            },
        }
    )


def mark_as_duplicate(finding: Finding, original: Finding, key: str) -> Finding:
    """Return a copy of ``finding`` marked as a duplicate of ``original``.

    The dedupe rationale records *why* the merge happened: same critic, same target identity,
    same target scope. It never references prose similarity.
    """

    critic_id = finding.critic_runs[0].critic_id if finding.critic_runs else "unknown"

    return finding.model_copy(
        update={
            "status": FindingStatus.DUPLICATE,
            "duplicate_of": original.id,
            "dedupe_key": key,
            "dedupe_rationale": (
                f"Same critic ({critic_id}) "
                f"already produced finding {original.id} for this target identity and scope. "
                "Deduplication is identity-based (critic, target, scope); prose similarity is "
                "never used to merge findings."
            ),
        }
    )


def find_original(*, session: Session, key: str) -> Finding | None:
    """Return the original finding recorded under ``key``, if any."""

    return FindingRepository(session).find_by_dedupe_key(key)
