"""Reading a spec into clauses, and proposing a verdict from a clause's own wording.

Two functions, both deterministic and both deliberately unimpressive.

**Parsing.** A spec is markdown, and the useful unit is the *clause* — a line or bullet that
states a requirement. Headings are tracked so each clause carries the section it came from,
which is what makes a generated case traceable back to the contract it tests.

**Labelling.** The verdict a case should produce is read out of the clause's own normative
wording: a clause about a violation proposes ``finding``, one about a satisfied requirement
proposes ``none``, one about an undeterminable outcome proposes ``inconclusive``.

This is a *proposal*, never a decision, and the distinction is the whole point. A label
derived from the same text as the case's inputs is the source grading itself. So
:func:`propose_label` returns a proposal that a reviewer must confirm before the case is
scored, and it returns ``None`` for any clause whose wording carries no signal at all
rather than guessing. A clause it cannot classify is skipped; a clause it misclassifies is
a finding about the rule, which is why every proposal is recorded with its rule name.

The keyword sets are conservative on purpose. An over-broad set manufactures labelled cases
for requirements that say nothing about an outcome, and each of those is a false label in
the golden set — worse than a gap, because a gap is visible and a false label is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from nyx.domain.base import NyxModel
from nyx.evals.schema import ExpectedVerdict

#: Which keyword set decided a label, recorded so a reviewer can see *why* a case was
#: proposed the way it was rather than taking the label on trust.
LabelRule = Literal["ambiguity", "violation", "satisfied"]

#: A clause must read like a requirement before it is worth a case. Without this, prose
#: about the project's history or its roadmap becomes "clauses" and every generated case is
#: about something no critic assesses.
_NORMATIVE = re.compile(
    r"\b("
    r"must\b|shall\b|required\b|requires\b|forbidden\b|prohibited\b|must not\b|"
    r"is a finding\b|raises a finding\b|violat\w*\b|breach\w*\b|"
    r"unauthorised\b|unauthorized\b|never\b|always\b"
    r")",
    re.IGNORECASE,
)

#: An undeterminable outcome. Checked **first**: "returns INCONCLUSIVE — never a verdict"
#: contains "never", and the specific word must win over the generic one.
_AMBIGUOUS: re.Pattern[str] = re.compile(
    r"\b(inconclusive|abstain\w*|cannot be determined|not comparable|unproven|confounded)\b",
    re.IGNORECASE,
)

#: A requirement whose violation is the outcome.
_VIOLATION: re.Pattern[str] = re.compile(
    r"\b("
    r"violat\w*|breach\w*|forbidden|prohibit\w*|must not|unauthorised|unauthorized|"
    r"missing\b|contradict\w*|unsupported\b|stale\b|out of scope|exceeds\b|"
    r"drift\w*|recur\w*|is a finding|raises a finding|without (?:an )?authoris|"
    r"without (?:an )?approv|no linked|not covered|does not cover"
    r")\b",
    re.IGNORECASE,
)

#: A requirement whose satisfaction is the outcome.
#:
#: Deliberately restricted to *outcome* words. ``authorised``/``authorized``/``approved``
#: were here and had to be removed: they describe an **actor**, so a clause reading "must be
#: authorized by a separately defined governance policy" was labelled ``none`` -- a
#: requirement reported as a satisfied outcome. A word that can describe either side of a
#: requirement cannot decide which side the clause is on.
_SATISFIED: re.Pattern[str] = re.compile(
    r"\b("
    r"no finding|passes\b|pass\b|satisf\w*|complie\w*|complies\b|valid\b|"
    r"in scope|within scope|covered by|resolves\b|consistent with"
    r")\b",
    re.IGNORECASE,
)

#: Rules in the order they are applied, so a new signal is added in exactly one place.
_RULES: tuple[tuple[LabelRule, re.Pattern[str], ExpectedVerdict], ...] = (
    ("ambiguity", _AMBIGUOUS, ExpectedVerdict.INCONCLUSIVE),
    ("violation", _VIOLATION, ExpectedVerdict.FINDING),
    ("satisfied", _SATISFIED, ExpectedVerdict.NONE),
)

#: Text inside quotes is an *example*, not the requirement's own claim about an outcome.
#: "Detect a claim whose wording is stronger ... e.g. 'all tests pass' supported by one run"
#: is a requirement about over-strong wording, and the quoted phrase is the bad example. A
#: keyword rule that reads the quote as the clause's signal labels the requirement backwards,
#: so quoted spans are removed before any signal is matched.
_QUOTED = re.compile(r"[\"“]([^\"”]*)[\"”]|\u2018([^\u2019]*)\u2019|`([^`]*)`")


def _strip_quotes(clause: str) -> str:
    """Return the clause with quoted spans blanked, so an example cannot carry the signal."""

    return _QUOTED.sub(lambda m: " ", clause)


_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*```")
_MIN_CLAUSE_CHARS = 24


class SpecClause(NyxModel):
    """One normative clause, with the spec section it was read from.

    ``ref`` is the section path joined by `` > ``, so a case can point at ``"8 > C-001"``
    rather than at a line number that shifts every time the document is edited.
    """

    ref: str
    section: str
    text: str


@dataclass(frozen=True, slots=True)
class ProposedLabel:
    """A label proposed from a clause, plus the rule that proposed it.

    ``rule`` is recorded so a reviewer can disagree with the *reason* rather than only the
    answer, and ``confidence`` is always ``low`` for now: the deterministic rule is a drafting
    aid, and claiming a confidence it has not earned would defeat the review step.
    """

    verdict: ExpectedVerdict
    rule: LabelRule
    confidence: Literal["low"] = "low"


def parse_spec(text: str, *, source: str = "<memory>") -> list[SpecClause]:
    """Split a markdown spec into normative clauses.

    Headings, fenced code blocks, and table rows are not clauses. A clause must carry a
    normative keyword and be long enough to state a requirement, so an acronym list or a
    changelog line does not become a case.
    """

    clauses: list[SpecClause] = []
    headings: list[str] = []
    in_fence = False

    for raw in text.splitlines():
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        heading = _HEADING.match(raw)
        if heading is not None:
            headings.append(heading.group(1).strip())
            continue
        if not raw.strip() or raw.lstrip().startswith(("|", ">", "<!--")):
            continue

        line = _BULLET.sub("", raw).strip()
        if len(line) < _MIN_CLAUSE_CHARS or not _NORMATIVE.search(line):
            continue
        # A spec nests deeply, and a ref that walks six headings is not a ref a reader can
        # scan. The source plus the three most specific headings locate the clause without
        # reproducing its whole ancestry.
        ref = " > ".join([source, *headings[-3:]])
        clauses.append(SpecClause(ref=ref, section=ref, text=line))

    return clauses


def propose_label(clause: str) -> ProposedLabel | None:
    """Propose a verdict for a clause from its own normative wording.

    Returns ``None`` when the clause carries no outcome signal, and also when it carries
    **conflicting** signals — a clause that says both "is a finding" and "passes" cannot be
    labelled by a keyword rule, and picking the first match would hide that the clause is
    about two different outcomes. A clause it cannot classify is skipped; a clause it
    misclassifies is a false label in the golden set, which is worse than a visible gap.
    """

    stripped = _strip_quotes(clause)
    matched = {rule: pattern.search(stripped) is not None for rule, pattern, _ in _RULES}
    hits = [rule for rule, found in matched.items() if found]
    if not hits:
        return None
    if len(hits) > 1:
        return None
    for rule, _pattern, verdict in _RULES:
        if matched[rule]:
            return ProposedLabel(verdict=verdict, rule=rule)
    return None  # pragma: no cover - unreachable: hits is non-empty
