"""A provider may wrap its answer in a markdown fence. That is a wrapper, not a refusal.

These tests pin the boundary the extraction must hold: it removes the wrapper and nothing else.
It does not repair malformed JSON and it does not widen what a caller accepts -- the caller's
schema is still enforced on the result, so a fenced document that violates the contract is
rejected exactly as an unfenced one would be.
"""

from __future__ import annotations

import json

import pytest

from nyx.llm.provider import extract_json_document


def test_a_bare_document_is_returned_unchanged() -> None:
    document = '{"result": "no_finding"}'

    assert json.loads(extract_json_document(document)) == {"result": "no_finding"}


@pytest.mark.parametrize("language", ["json", "JSON", "json5", ""])
def test_a_fenced_document_has_its_wrapper_removed(language: str) -> None:
    fenced = f'```{language}\n{{"result": "finding"}}\n```'

    assert json.loads(extract_json_document(fenced)) == {"result": "finding"}


def test_a_fence_without_a_newline_is_not_treated_as_a_wrapper() -> None:
    """`` ```json {...} ``` `` on one line is prose, not a fenced block."""

    text = '```json {"result": "finding"} ```'

    # The span is still found, because extraction falls back to the first balanced document.
    assert json.loads(extract_json_document(text)) == {"result": "finding"}


def test_prose_around_the_document_is_ignored() -> None:
    text = 'Here is my answer:\n```json\n{"result": "none"}\n```\nI hope that helps.'

    assert json.loads(extract_json_document(text)) == {"result": "none"}


def test_an_array_document_is_found() -> None:
    assert json.loads(extract_json_document("```\n[1, 2, 3]\n```")) == [1, 2, 3]


def test_a_brace_inside_a_string_does_not_end_the_span() -> None:
    document = '{"rationale": "the payload contains a } and a {", "result": "none"}'

    assert json.loads(extract_json_document(document))["result"] == "none"


def test_an_escaped_quote_does_not_end_the_string() -> None:
    document = r'{"rationale": "she said \"stop\"", "result": "none"}'

    assert json.loads(extract_json_document(document))["result"] == "none"


def test_an_unclosed_document_is_refused() -> None:
    with pytest.raises(ValueError, match="no JSON document"):
        extract_json_document('{"result": "finding"')


def test_text_with_no_json_at_all_is_refused() -> None:
    """A refusal is not a pass-through: it hides the failure if returned unchanged."""

    with pytest.raises(ValueError, match="no JSON document"):
        extract_json_document("I cannot assess this claim.")


def test_a_fenced_document_that_is_not_json_is_refused() -> None:
    with pytest.raises(ValueError, match="no JSON document"):
        extract_json_document("```json\nnot json at all\n```")


def test_extraction_does_not_repair_malformed_json() -> None:
    """The wrapper is removed; the parse still fails. Extraction is not a repair."""

    with pytest.raises(json.JSONDecodeError):
        json.loads(extract_json_document('```json\n{"a": 1,}\n```'))
