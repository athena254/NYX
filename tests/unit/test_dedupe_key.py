"""Dedupe key stability (SPEC §11.2).

The key must be a pure function of the (critic, target, scope) identity: two calls over the same
triple must agree, and the same triple described with a reordered dict must agree too.

The period case is the one that matters. ``Scope.period`` holds ``datetime`` objects, and
``canonical_json`` is the hashing primitive -- so before the fix, any finding whose scope narrowed
by *time* failed to compose with ``TypeError: Object of type datetime is not JSON serializable``.
The finding was lost, not merely mislabelled.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nyx.domain.base import Period, Scope
from nyx.domain.canonical import canonical_json, content_hash
from nyx.findings.dedup import dedupe_key


def _period() -> Period:
    return Period(
        from_=datetime(2026, 10, 19, 10, 0, tzinfo=UTC),
        to=datetime(2026, 10, 20, 10, 0, tzinfo=UTC),
    )


def test_dedupe_key_is_stable_for_the_same_scope() -> None:
    scope = Scope(system_id="sys_demo", artifact="demo", period=_period())

    first = dedupe_key(
        critic_id="c001-completion-vs-verification",
        target_type="claim",
        target_id="clm_demo",
        scope=scope,
    )
    second = dedupe_key(
        critic_id="c001-completion-vs-verification",
        target_type="claim",
        target_id="clm_demo",
        scope=scope,
    )

    assert first == second
    assert first.startswith("sha256:")


def test_dedupe_key_differs_when_the_period_differs() -> None:
    """Two claims about the same artifact in different windows are different findings."""

    earlier = Scope(system_id="sys_demo", artifact="demo", period=_period())
    later = Scope(
        system_id="sys_demo",
        artifact="demo",
        period=Period(from_=datetime(2026, 11, 1, tzinfo=UTC)),
    )

    assert dedupe_key(
        critic_id="c001-completion-vs-verification",
        target_type="claim",
        target_id="clm_demo",
        scope=earlier,
    ) != dedupe_key(
        critic_id="c001-completion-vs-verification",
        target_type="claim",
        target_id="clm_demo",
        scope=later,
    )


def test_dedupe_key_differs_when_the_critic_differs() -> None:
    scope = Scope(system_id="sys_demo", artifact="demo", period=_period())

    assert dedupe_key(
        critic_id="c001-completion-vs-verification",
        target_type="claim",
        target_id="clm_demo",
        scope=scope,
    ) != dedupe_key(
        critic_id="c005-policy-violation",
        target_type="claim",
        target_id="clm_demo",
        scope=scope,
    )


def test_scope_with_a_period_canonicalises_and_hashes() -> None:
    """The primitive the dedupe key is built from must accept a period scope at all."""

    scope = Scope(system_id="sys_demo", artifact="demo", period=_period())

    payload = {"scope": scope}
    digest = content_hash(payload)

    assert digest.startswith("sha256:")
    # The canonical form carries the domain's UTC ``Z`` representation, not a Python repr.
    assert b"2026-10-19T10:00:00Z" in canonical_json(payload)


def test_canonical_json_rejects_a_value_it_cannot_encode() -> None:
    """The default handler must not silently swallow anything: an unknown type still fails."""

    with pytest.raises(TypeError, match="not JSON serializable"):
        canonical_json({"value": object()})
