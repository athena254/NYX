"""Turn transcripts and records into discrete, typed claims.

Deterministic patterns first. The extractor is a set of explicit regexes over text spans, and it
returns the **span it matched** alongside every claim. The span is the point: a claim extracted
from a transcript must be traceable back to the words that produced it, or "the agent claimed X"
is an assertion about an agent rather than a reading of it.

An LLM may assist later. It is deliberately out of scope here, and the seam for it is
:class:`ClaimExtractor` itself: an alternative implementation can be injected without changing the
callers. Any such implementation must return schema-validated output and keep the source span,
which is why :class:`ExtractedClaim` carries ``span`` as a required field rather than an optional
one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from nyx.domain.base import Scope
from nyx.domain.enums import ClaimType

#: Minimum length of a candidate statement. Below this a span is chatter ("ok", "let me check")
#: rather than a proposition, and recording it as a claim would flood the store with unfalsifiable
#: statements. The agent-transcript adapter uses the same threshold for the same reason.
MIN_STATEMENT_LENGTH = 12

_COMPLETION = re.compile(
    r"\b(?:is|are|was|were|has been|have been)?\s*"
    r"(?:done|complete(?:d)?|finished|fixed|resolved|deployed|shipped|released|merged|"
    r"passed|green|verified|working)\b",
    re.IGNORECASE,
)
_TEST_RESULT = re.compile(
    r"\b(?:all\s+)?(?:the\s+)?tests?\s+(?:are\s+)?(?:pass(?:ed|ing|es)?|fail(?:ed|ing|s)?|green|red)\b",
    re.IGNORECASE,
)
_FIX = re.compile(
    r"\b(?:fixed|patched|resolved|reverted|rolled back|worked around)\s+"
    r"(?:the\s+)?[a-z0-9_ -]{3,60}",
    re.IGNORECASE,
)
_DEPLOY = re.compile(
    r"\b(?:deployed|released|shipped|published)\s+(?:to|into)?\s*[a-z0-9_.:/-]{3,80}",
    re.IGNORECASE,
)
_SIDE_EFFECT = re.compile(
    r"\b(?:saved|created|written|inserted|queued|sent|updated|deleted)\s+(?:the\s+|a\s+|an\s+)?"
    r"[a-z0-9_ -]{3,60}",
    re.IGNORECASE,
)
_UNIVERSAL = re.compile(
    r"\b(all|every|always|never|none|100%|guaranteed|fully|completely|entire)\b",
    re.IGNORECASE,
)

#: Ordered: the first pattern that matches a span decides its type. Completion is checked first
#: because "fixed" and "passed" are both completion claims and the distinction between "fixed"
#: and "tests pass" is not worth a second claim about the same span.
_PATTERNS: tuple[tuple[re.Pattern[str], ClaimType], ...] = (
    (_TEST_RESULT, ClaimType.QUALITY),
    (_FIX, ClaimType.COMPLETION),
    (_DEPLOY, ClaimType.COMPLETION),
    (_SIDE_EFFECT, ClaimType.FACTUAL),
    (_COMPLETION, ClaimType.COMPLETION),
)

#: The ceiling on a stored claim statement. ``Claim.statement`` enforces it at the domain
#: boundary, so an extraction that produced a longer sentence is refused at persistence rather
#: than silently truncated here.
MAX_STATEMENT_LENGTH = 2000

#: A sentence boundary. Deliberately simple: transcripts are prose, and a full sentence parser
#: would add a dependency to solve a problem a regex already solves. An abbreviation such as
#: "e.g." will split early; the cost is a claim whose span is slightly short, never a wrong one.
_SENTENCE_BOUNDARY = re.compile(r"[.!?]+\s+")


def _sentences(source_text: str) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` offsets of every sentence in ``source_text``.

    The span includes the terminating punctuation and **excludes** the whitespace that follows it,
    so a claim's text is exactly the words a reader would quote and never carries a trailing space
    it did not earn.
    """

    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(source_text):
        end = match.end()
        while end > start and source_text[end - 1].isspace():
            end -= 1
        spans.append((start, end))
        start = match.end()
    while start < len(source_text) and source_text[start].isspace():
        start += 1
    if start < len(source_text):
        spans.append((start, len(source_text)))
    return spans


def _sentence_containing(
    sentences: list[tuple[int, int]], start: int, end: int
) -> tuple[int, int] | None:
    """Return the sentence span that contains ``[start, end)``, if any."""

    for sentence_start, sentence_end in sentences:
        if sentence_start <= start and end <= sentence_end:
            return (sentence_start, sentence_end)
    return None


@dataclass(frozen=True, slots=True)
class ExtractedClaim:
    """One proposition read out of a span of source text.

    ``span`` is the exact ``(start, end)`` offsets into ``source_text``. ``text`` is the span's
    own text, not a paraphrase: a claim whose text differs from its span cannot be audited.
    """

    text: str
    claim_type: ClaimType
    span: tuple[int, int]
    source_text: str
    source_ref: str
    certainty_markers: tuple[str, ...] = ()

    @property
    def universal(self) -> bool:
        """Whether the span carries a universal quantifier (C-008's trigger)."""

        return bool(_UNIVERSAL.search(self.text))

    def scoped(self, scope: Scope) -> Scope:
        """Return the scope this claim should be stored under.

        Extraction does not invent scope. When the caller supplies one it is used unchanged; the
        claim carries only what the source actually said.
        """

        return scope


class ClaimExtractor(Protocol):
    """The seam an LLM-assisted extractor would also implement."""

    def extract(self, *, source_text: str, source_ref: str) -> list[ExtractedClaim]:
        """Return every claim found in ``source_text``, with its span."""
        ...


class RegexClaimExtractor:
    """Deterministic claim extraction from explicit patterns.

    A claim is the **sentence** that contains a match, not the match itself. That choice is what
    makes short but unambiguous statements extractable ("is done", "tests passed") while keeping
    the span auditable: the span is a unit a reader would quote, and the certainty markers are
    read from the same span, so "The tests passed 100%." carries its own overconfidence.

    Deliberately conservative in both directions:

    * it does **not** extract from a sentence no pattern matches, so a vague sentence becomes no
      claim rather than a guessed one;
    * it does **not** extract a question, because an interrogative is not an assertion;
    * it **does** return two claims for one sentence when two different patterns match it, because
      "the tests pass" and "the build is green" in one sentence are two propositions an agent
      could be wrong about independently.
    """

    def extract(self, *, source_text: str, source_ref: str) -> list[ExtractedClaim]:
        sentences = _sentences(source_text)
        found: list[ExtractedClaim] = []
        claimed_spans: list[tuple[int, int]] = []
        for sentence_start, sentence_end in sentences:
            sentence = source_text[sentence_start:sentence_end]
            if len(" ".join(sentence.split())) < MIN_STATEMENT_LENGTH:
                continue
            if sentence.rstrip().endswith("?"):
                continue
            for pattern, claim_type in _PATTERNS:
                if not pattern.search(sentence):
                    continue
                span = (sentence_start, sentence_end)
                if span in claimed_spans and pattern is _COMPLETION:
                    # The generic completion pattern and a specific one can both match the same
                    # sentence; the specific one already claimed it, and a second identical claim
                    # would be noise rather than information.
                    continue
                claimed_spans.append(span)
                text = " ".join(sentence.split())
                found.append(
                    ExtractedClaim(
                        text=text,
                        claim_type=claim_type,
                        span=span,
                        source_text=source_text,
                        source_ref=source_ref,
                        certainty_markers=_certainty_markers(text),
                    )
                )
        return sorted(found, key=lambda item: item.span[0])

    def extract_from_many(self, *, sources: Iterable[tuple[str, str]]) -> list[ExtractedClaim]:
        """Extract from several sources, keeping each source's reference."""

        found: list[ExtractedClaim] = []
        for source_text, source_ref in sources:
            found.extend(self.extract(source_text=source_text, source_ref=source_ref))
        return found


def _certainty_markers(text: str) -> tuple[str, ...]:
    """Return the certainty markers present in ``text``, strongest first.

    Used by the upgraded C-008, which compares the certainty in the *claim* against the strength
    of what backs it. Extracted here rather than re-derived there so both read one list.
    """

    markers: list[str] = []
    lowered = text.lower()
    for marker in ("100%", "guaranteed", "definitely", "certainly", "fully", "completely"):
        if marker in lowered:
            markers.append(marker)
    return tuple(markers)


def default_extractor() -> ClaimExtractor:
    """Return the deterministic extractor."""

    return RegexClaimExtractor()
