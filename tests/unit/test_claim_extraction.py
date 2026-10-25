"""Tests for deterministic claim extraction from transcripts and records.

The invariant under test is traceability: every claim the extractor returns must be auditable back
to the exact words that produced it. A claim whose text is not its own span is an assertion about
an agent, not a reading of one, and the whole ground-truth layer rests on the difference.
"""

from __future__ import annotations

from nyx.domain.enums import ClaimType
from nyx.evidence.claims import (
    MAX_STATEMENT_LENGTH,
    MIN_STATEMENT_LENGTH,
    RegexClaimExtractor,
    _certainty_markers,
    default_extractor,
)

_EXTRACTOR = RegexClaimExtractor()


def _spans(source: str) -> list[tuple[int, int]]:
    return [claim.span for claim in _EXTRACTOR.extract(source_text=source, source_ref="t:1")]


def _claims(source: str) -> list[tuple[str, ClaimType]]:
    return [
        (claim.text, claim.claim_type)
        for claim in _EXTRACTOR.extract(source_text=source, source_ref="t:1")
    ]


# --- traceability ---------------------------------------------------------


def test_every_extracted_claim_text_is_exactly_its_span() -> None:
    """The claim must be a reading of the source, not a paraphrase of it."""

    source = "I fixed the parser. The tests passed after that."
    for claim in _EXTRACTOR.extract(source_text=source, source_ref="t:1"):
        start, end = claim.span
        assert source[start:end] == " ".join(claim.text.split())
        assert claim.source_text == source


def test_spans_are_ordered_and_never_regress() -> None:
    """Two propositions in one sentence share its span; spans never go backwards.

    Sharing is correct: "the tests pass and the build is green" is two things an agent could be
    wrong about independently. What must not happen is a span that overlaps a *different*
    sentence's span.
    """

    source = "The tests passed and I fixed the parser and deployed to staging."
    starts = [start for start, _ in _spans(source)]
    assert starts == sorted(starts)
    # One sentence, several propositions: every span is that one sentence.
    assert set(_spans(source)) == {(0, len(source))}


def test_claims_are_returned_in_source_order() -> None:
    source = "Deployed to staging. The tests passed. Fixed the lexer."
    spans = _spans(source)
    assert spans == sorted(spans)


def test_the_source_reference_is_recorded_on_every_claim() -> None:
    for claim in _EXTRACTOR.extract(source_text="The tests passed.", source_ref="session:42"):
        assert claim.source_ref == "session:42"


# --- what gets extracted, and how it is typed ------------------------------


def test_a_completion_claim_is_typed_completion() -> None:
    claims = _claims("The booking flow is done.")
    assert ("The booking flow is done.", ClaimType.COMPLETION) in claims


def test_a_fix_is_typed_completion() -> None:
    claims = _claims("I fixed the retry logic.")
    assert ("I fixed the retry logic.", ClaimType.COMPLETION) in claims


def test_a_test_result_is_typed_quality_not_completion() -> None:
    """A test outcome is a quality statement; "fixed" is a completion statement.

    The distinction matters because C-008 reads universal wording in claims, and a test report
    says something different from a fix.
    """

    claims = _claims("The tests passed.")
    assert claims == [("The tests passed.", ClaimType.QUALITY)]


def test_a_side_effect_claim_is_typed_factual() -> None:
    claims = _claims("I saved the booking record.")
    assert ("I saved the booking record.", ClaimType.FACTUAL) in claims


def test_a_deploy_claim_is_typed_completion() -> None:
    claims = _claims("Deployed to staging.")
    assert ("Deployed to staging.", ClaimType.COMPLETION) in claims


def test_one_sentence_can_yield_more_than_one_claim() -> None:
    """Two propositions an agent could be wrong about independently are two claims."""

    source = "The tests passed and I deployed to staging."
    assert len(_EXTRACTOR.extract(source_text=source, source_ref="t:1")) >= 2


def test_a_failure_statement_is_still_extracted() -> None:
    """An agent reporting a failure is a claim too, and the critic needs to see it."""

    source = "The tests failed after the change."
    claims = _claims(source)
    assert ("The tests failed after the change.", ClaimType.QUALITY) in claims


# --- what must NOT be extracted -------------------------------------------


def test_chatter_is_not_extracted() -> None:
    """ "ok" and "let me check" are not propositions; storing them floods the store."""

    for chatter in ("ok", "let me check", "thanks", "I will look"):
        assert _EXTRACTOR.extract(source_text=chatter, source_ref="t:1") == []


def test_a_question_is_not_extracted() -> None:
    assert _EXTRACTOR.extract(source_text="Did the tests pass?", source_ref="t:1") == []


def test_an_unclassifiable_statement_is_not_extracted() -> None:
    """No pattern, no claim: guessing a type would manufacture a finding."""

    source = "The colour of the dashboard is blue."
    assert _EXTRACTOR.extract(source_text=source, source_ref="t:1") == []


def test_a_short_but_unambiguous_statement_is_extracted() -> None:
    """Sentence-level spans make short claims extractable without making chatter extractable.

    "Tests passed" is only twelve characters, but it is an assertion about a state of the world,
    and dropping it would lose a claim a critic should be able to test. Chatter is filtered by
    the patterns, not by the length.
    """

    assert MIN_STATEMENT_LENGTH == 12
    claims = _claims("Tests passed")
    assert ("Tests passed", ClaimType.QUALITY) in claims


def test_a_short_completion_is_extracted() -> None:
    assert ("The task is done.", ClaimType.COMPLETION) in _claims("The task is done.")


def test_extraction_is_case_insensitive() -> None:
    claims = _claims("FIXED the parser.")
    assert any(text.startswith("FIXED") for text, _ in claims)


# --- certainty markers (the upgraded C-008 reads these) --------------------


def test_universal_markers_are_detected() -> None:
    claim = _EXTRACTOR.extract(source_text="All tests passed.", source_ref="t:1")[0]
    assert claim.universal is True
    assert claim.certainty_markers == ()


def test_strong_certainty_markers_are_detected() -> None:
    source = "The tests passed 100%."
    claim = _EXTRACTOR.extract(source_text=source, source_ref="t:1")[0]
    assert "100%" in claim.certainty_markers


def test_certainty_markers_are_returned_strongest_first() -> None:
    assert _certainty_markers("this is 100% guaranteed and fully verified") == (
        "100%",
        "guaranteed",
        "fully",
    )


def test_no_markers_yields_an_empty_tuple() -> None:
    assert _certainty_markers("the tests passed") == ()


# --- bounds ---------------------------------------------------------------


def test_a_very_long_sentence_is_not_truncated_by_the_extractor() -> None:
    """Truncating would break the span/text equality that makes a claim auditable.

    The ceiling is enforced at the domain boundary instead: ``Claim.statement`` rejects a
    statement longer than ``MAX_STATEMENT_LENGTH``, so an over-long sentence is refused at
    persistence rather than silently shortened into something the source never said.
    """

    padding = "x" * (MAX_STATEMENT_LENGTH + 500)
    source = f"I fixed the {padding}"
    for claim in _EXTRACTOR.extract(source_text=source, source_ref="t:1"):
        assert claim.text == source[slice(*claim.span)]
        assert len(claim.text) > MAX_STATEMENT_LENGTH


# --- the seam ------------------------------------------------------------


def test_the_default_extractor_is_the_deterministic_one() -> None:
    """An LLM-assisted extractor is a different implementation of the same seam, not a new one."""

    assert isinstance(default_extractor(), RegexClaimExtractor)


def test_extract_from_many_keeps_each_source_reference() -> None:
    sources = [("The tests passed.", "s:1"), ("Fixed the parser.", "s:2")]
    claims = _EXTRACTOR.extract_from_many(sources=sources)
    assert {claim.source_ref for claim in claims} == {"s:1", "s:2"}


def test_an_empty_source_yields_no_claims() -> None:
    assert _EXTRACTOR.extract(source_text="", source_ref="t:1") == []
