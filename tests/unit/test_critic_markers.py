"""Regression: the success/failure markers must recognise the third-person singular forms.

Both marker patterns used ``\\bpass(?:ed|ing)?\\b`` and ``\\bfail(?:ed|ing|ure)?\\b``. Because the
trailing ``\\b`` is a *word boundary*, the forms "passes" and "fails" did not match: after
"pass"/"fail" comes "s", which is still a word character, so no boundary exists there.

That silently broke two of the most natural phrasings:

- "the suite passes for commit abc123" read as having **no stance**, so C-001 skipped it as a
  non-assertion instead of assessing the completion claim at all.
- "the build fails" read as having no stance, so C-002 could not see the contradiction against
  a passing claim about the same scope.

These are false negatives in the detection vocabulary, not scope errors, so the fix belongs in
the patterns rather than in the critics' logic. The patterns stay deliberately word-bounded:
"passing" and "failure" must still match, and neutral prose must still match neither.
"""

from __future__ import annotations

import pytest

from nyx.critics.c001_completion_vs_verification import _FAILURE_MARKER as C001_FAILURE
from nyx.critics.c001_completion_vs_verification import _SUCCESS_MARKER as C001_SUCCESS
from nyx.critics.c002_contradictory_records import _FAILURE_MARKER as C002_FAILURE
from nyx.critics.c002_contradictory_records import _SUCCESS_MARKER as C002_SUCCESS

SUCCESS_PHRASINGS = [
    "the suite passes",
    "the build passes",
    "all tests passed",
    "tests are passing",
    "it is green",
]

FAILURE_PHRASINGS = [
    "the build fails",
    "it fails",
    "tests failed",
    "a test failure",
    "the suite is failing",
    "the build is red",
]

NEUTRAL_PHRASINGS = [
    "the build ran",
    "the suite executed",
    "the report was generated",
]


@pytest.mark.parametrize("text", SUCCESS_PHRASINGS)
def test_success_phrasings_are_recognised(text: str) -> None:
    assert C001_SUCCESS.search(text.lower()), f"C-001 missed success phrasing: {text!r}"
    assert C002_SUCCESS.search(text.lower()), f"C-002 missed success phrasing: {text!r}"


@pytest.mark.parametrize("text", FAILURE_PHRASINGS)
def test_failure_phrasings_are_recognised(text: str) -> None:
    assert C001_FAILURE.search(text.lower()), f"C-001 missed failure phrasing: {text!r}"
    assert C002_FAILURE.search(text.lower()), f"C-002 missed failure phrasing: {text!r}"


@pytest.mark.parametrize("text", NEUTRAL_PHRASINGS)
def test_neutral_text_matches_neither_marker(text: str) -> None:
    """The widened patterns must not start matching ordinary prose."""

    assert not C001_FAILURE.search(text.lower()), f"C-001 false-positive on {text!r}"
    assert not C002_FAILURE.search(text.lower()), f"C-002 false-positive on {text!r}"
    assert not C001_SUCCESS.search(text.lower()), f"C-001 false-positive on {text!r}"
    assert not C002_SUCCESS.search(text.lower()), f"C-002 false-positive on {text!r}"


def test_stance_reads_the_s_forms_end_to_end() -> None:
    """Prove the fix at the level the critic actually consumes: a resolved stance."""

    from nyx.critics.c002_contradictory_records import _stance
    from nyx.domain.base import Scope
    from nyx.domain.claims import Claim
    from nyx.domain.enums import ClaimType

    def claim(statement: str) -> Claim:
        return Claim(
            statement=statement,
            claimant_system_id="sys_marker",
            claim_type=ClaimType.COMPLETION,
            scope=Scope(
                system_id="sys_marker",
                artifact="demo",
                commit="abc123",
                environment="development",
            ),
        )

    assert _stance(claim("The suite passes.")) == "success"
    assert _stance(claim("The build fails.")) == "failure"
    assert _stance(claim("The build ran.")) is None
