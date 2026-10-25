"""C-004 — Provenance gap (SPEC §8).

Flags material claims whose linked evidence lacks a source, source identity, locator,
capture time, or scope. The finding **names the missing fields** and is worded as a
completeness gap: it never implies malicious behaviour. A record with missing provenance is
harder to audit; it is not evidence of wrongdoing.

Four rules:

* RULE 1 (baseline provenance): the evidence declares a source, a producer identity, a
  locator, and a scope. This is the pre-ground-truth rule set and it is unchanged.
* RULE 2 (verification evidence): evidence that declares a verification method, or whose kind
  is ``TEST_RESULT``/``CI_TEST_RESULT``, must also carry a subject version and a content hash.
  A test result is the record a claim is verified by, so an unhashed, undated one is not
  auditable even when its other provenance is complete.
* RULE 3 (origin must not be entirely unverifiable): evidence of any other kind must carry at
  least one of a producer identity or a content hash. Evidence with neither is unattributed
  *and* unverifiable, which is a stronger gap than a missing locator.
* RULE 4 (hash chain): when the evidence declares a ``content_hash`` and a linked record
  declares the hash it was derived from (``chain_hash``/``prior_hash``), a mismatch is a
  finding. With no chain declared anywhere this rule is a no-op — chaining is optional.

**Deliberately conservative.** RULE 2 and RULE 3 fire only on the kind that makes the
requirement applicable, and RULE 3 accepts either field rather than demanding both: a record
with a producer but no hash is attributable, and a record with a hash but no producer is at
least integrity-checkable. Requiring a hash on every kind of evidence would manufacture
findings on records that have never carried one, so the new rules are gated by
``require_verifiable_origin`` and off by default; with the flag off this critic behaves
exactly as it did before the ground-truth layer existed.

``CRITIC_VERSION`` was bumped from ``1.0.0`` to ``1.1.0`` to record RULE 2, RULE 3 and RULE 4:
they can only add findings, never remove one, so the 1.0.0 behaviour is the flag-off state of
this same rule set.

Limitation: this critic checks that the *fields are present*, not that the values are true. A
producer identity is self-asserted, a content hash proves only that the recorded bytes match a
recorded digest, and a chain link proves only that two recorded digests agree. None of them
establishes that the evidence is honest, complete, or about the claim it is linked to. A
verification method declared as a free-text string is not checked against a known method
vocabulary, so an unrecognised method is not a violation.

A ``no_finding`` result means **only** that this critic found no issue within its declared
scope. It is never a statement that the evidence is authentic, that the claim is true, or that
no other critic could find something.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Scope
from nyx.domain.enums import CriticResultStatus, CriticType, EvidenceKind, FindingCategory, Severity
from nyx.domain.evidence import EvidenceItem

CRITIC_ID = "c004-provenance-gap"
CRITIC_VERSION = "1.1.0"

_VERIFICATION_KINDS = frozenset({EvidenceKind.TEST_RESULT, EvidenceKind.CI_TEST_RESULT})

#: Keys a linked record may use to name the digest an evidence item was derived from. Both names
#: are accepted because the audit chain and the evidence payload each use one of them.
_CHAIN_KEYS = ("chain_hash", "prior_hash")

_REQUIRED_PROVENANCE: tuple[tuple[str, str], ...] = (
    ("source", "a source_system_id or source_name"),
    ("source identity", "producer_identity"),
    ("locator", "locator"),
    ("capture time", "captured_at"),
    ("scope", "scope"),
)


def _no_finding(proposition: str, rationale: str, scope: Scope) -> CriticResult:
    """Build a ``no_finding`` result.

    ``no_finding`` means only that this critic found no issue within its declared scope. It
    is never a statement that the evidence is authentic or that the claim is true.
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


def _missing_fields(evidence: EvidenceItem) -> list[str]:
    """Return the human-readable names of the provenance fields the evidence is missing.

    ``captured_at`` is deliberately absent from this list. It is a *required* field on
    ``EvidenceItem``, so ingestion rejects evidence that does not carry one — a missing capture
    time can never reach this critic. That is boundary enforcement rather than detection, and it
    is the stronger of the two: the gap is prevented instead of merely reported. The fields
    checked below are the genuinely optional ones, so they can be absent on a stored record.
    """

    missing: list[str] = []
    if evidence.source_system_id is None and evidence.source_name is None:
        missing.append("source")
    if evidence.producer_identity is None:
        missing.append("source identity")
    if evidence.locator is None:
        missing.append("locator")
    if evidence.scope is None:
        missing.append("scope")
    return missing


def _verification_gaps(evidence: EvidenceItem) -> list[str]:
    """RULE 2: verification evidence must also declare a subject version and a content hash.

    Applies only when the evidence declares a verification method or is a verification kind.
    The subject version is read from the scope's commit, because that is where this model
    records the version the evidence applies to.
    """

    declares_method = evidence.verification is not None and bool(evidence.verification.method)
    if not declares_method and evidence.kind not in _VERIFICATION_KINDS:
        return []
    missing: list[str] = []
    if evidence.scope is None or not evidence.scope.commit:
        missing.append("subject version")
    if evidence.content_hash is None:
        missing.append("content hash")
    return missing


def _origin_gaps(evidence: EvidenceItem) -> list[str]:
    """RULE 3: evidence of any other kind must not be entirely unverifiable.

    At least one of a producer identity or a content hash must be present. Either is enough:
    attribution without a hash is auditable, and a hash without attribution is at least
    integrity-checkable. Only the record with neither is flagged.
    """

    if evidence.producer_identity is not None or evidence.content_hash is not None:
        return []
    return ["verifiable origin (producer_identity or content_hash)"]


def _chain_gaps(
    evidence: EvidenceItem,
    *,
    claim_chain: str | None,
    sibling_chains: Sequence[str | None],
) -> list[str]:
    """RULE 4: a declared chain link that does not match the evidence's content hash.

    The chain is read from the target event's payload and from the content hashes the linked
    evidence items declare, because a chain is a relation between records rather than a field on
    one of them. The comparison is exact and never inferred: a link must name the digest it was
    derived from. No chain declared anywhere is a no-op — chaining is optional and must never be
    required, so evidence with no hash and no declared link is simply not assessed by this rule.
    """

    if evidence.content_hash is None:
        return []
    declared = [value for value in [claim_chain, *sibling_chains] if value]
    if not declared:
        return []
    broken = [value for value in declared if value != evidence.content_hash]
    if not broken:
        return []
    return [
        f"hash chain link ({broken[0]}) does not match this evidence's content_hash "
        f"({evidence.content_hash})"
    ]


def _declared_chain(payload: dict[str, Any]) -> str | None:
    """Return the chain hash a payload declares, if any.

    Both key names are accepted because the audit chain and an evidence payload each use one.
    A declared value that is empty or not a string is treated as absent: an unrecognised value
    is not evidence of a broken chain.
    """

    for key in _CHAIN_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _declared_chain_payload(evidence: EvidenceItem) -> str | None:
    """Return the chain link a linked evidence record declares, if any.

    ``EvidenceItem`` has no payload field in this model, so the only declared digest on the
    evidence side is its own ``content_hash``. Returning ``None`` when there is none is the
    honest answer: a chain is verified only where a record actually declares one, and comparing
    a hash against prose such as ``source_ref`` or ``limitations`` would manufacture findings.
    """

    return evidence.content_hash


class ProvenanceGapCritic:
    """Check that linked evidence carries the provenance required to audit it.

    ``require_verifiable_origin`` switches on RULE 2 and RULE 3. It is a constructor argument
    rather than a module constant so a deployment can adopt the stronger requirement without a
    code change, and so the off-state is testable without monkeypatching. With the flag off
    this critic behaves exactly as it did before the ground-truth layer existed: a hash is not
    demanded of evidence that has never carried one.
    """

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Flag material claims whose evidence lacks a source, source identity, locator, "
        "capture time, or scope, naming the missing fields without implying malice; when "
        "origin verification is enabled, also require verification evidence to carry a subject "
        "version and a content hash, and every other kind to carry at least one of a producer "
        "identity or a content hash."
    )
    scope = CriticScope(
        can_assess=[
            "provenance completeness of evidence linked to a claim",
            "the subject version and content hash of verification evidence",
            "whether a declared hash chain link matches the evidence's content hash",
        ],
        cannot_assess=[
            "the authenticity or truth of evidence content",
            "the intent of the evidence producer",
            "whether a declared producer identity is genuine",
            "whether a declared content hash covers the content the claim relies on",
        ],
    )
    critic_type = CriticType.DETERMINISTIC

    def __init__(self, *, require_verifiable_origin: bool = False) -> None:
        self.require_verifiable_origin = require_verifiable_origin

    def _evidence_gaps(
        self,
        items: list[EvidenceItem],
        *,
        claim_chain: str | None,
        sibling_chains: Sequence[str | None],
    ) -> list[tuple[EvidenceItem, list[str]]]:
        """Collect the provenance gaps for every linked evidence item.

        The baseline fields are always checked. The origin rules (RULE 2 and RULE 3) apply only
        when the flag is on, because demanding a hash of evidence that has never carried one
        would manufacture findings on every existing record. RULE 4 (the hash chain) applies
        whenever a chain is declared, and is a no-op when none is.

        ``sibling_chains`` are the digests declared by the *other* linked records. An item is
        never compared against its own hash: a record that names its own digest is not a chain,
        it is a tautology, and treating it as one would make every hashed item look linked.
        """

        gaps: list[tuple[EvidenceItem, list[str]]] = []
        for item in items:
            missing = _missing_fields(item)
            if self.require_verifiable_origin:
                missing.extend(_verification_gaps(item))
                missing.extend(_origin_gaps(item))
            links = [
                value
                for value in [claim_chain, *sibling_chains]
                if value is not None and value != item.content_hash
            ]
            missing.extend(_chain_gaps(item, claim_chain=claim_chain, sibling_chains=links))
            if missing:
                gaps.append((item, missing))
        return gaps

    def run(self, context: CriticContext) -> CriticResult:
        claim = context.linked_claim
        if claim is None:
            return _no_finding(
                "No claim is linked to this event.",
                "This critic assesses the provenance of evidence linked to a claim; without a "
                "linked claim there is nothing to assess.",
                context.scope,
            )
        if not context.linked_evidence:
            return _no_finding(
                f"No evidence is linked to claim {claim.id}.",
                "This critic assesses the provenance of linked evidence; with no linked "
                "evidence there is nothing to assess within this critic's declared scope.",
                context.scope,
            )

        claim_chain = _declared_chain(context.target_event.payload)
        sibling_chains = [_declared_chain_payload(item) for item in context.linked_evidence]
        gaps = self._evidence_gaps(
            context.linked_evidence, claim_chain=claim_chain, sibling_chains=sibling_chains
        )
        if not gaps:
            return _no_finding(
                f"Evidence linked to claim {claim.id} carries the required provenance.",
                "Every linked evidence item declares a source, source identity, locator, "
                "capture time, and scope. This critic found no issue within its declared "
                "scope; provenance completeness is not authenticity.",
                context.scope,
            )

        observations = [
            f"Claim {claim.id} is linked to {len(context.linked_evidence)} evidence item(s)."
        ]
        for item, missing in gaps:
            observations.append(f"Evidence {item.id} is missing: {', '.join(missing)}.")
        named = "; ".join(f"{item.id} lacks {', '.join(missing)}" for item, missing in gaps)

        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=CriticResultStatus.FINDING,
            proposition=(
                f"Evidence linked to claim {claim.id} is missing required provenance fields."
            ),
            scope=claim.scope,
            observations=observations,
            inferences=[
                "The affected evidence items cannot be fully audited until the missing "
                "provenance is supplied.",
            ],
            limitations=[
                "This is a completeness gap in the evidence record, not an assertion of "
                "malicious behaviour by the producer.",
            ],
            rationale=(
                f"Claim {claim.id} is linked to evidence with incomplete provenance: {named}. "
                "The missing fields are named so the producer can supply them; no intent is "
                "implied."
            ),
            recommended_next_step=(
                "Ask the evidence producer to supply the missing provenance fields."
            ),
            category=FindingCategory.PROVENANCE_GAP,
            severity=Severity.LOW,
            title=f"Provenance gap in evidence linked to claim {claim.id}",
            summary=(
                f"Evidence linked to claim {claim.id} is missing provenance fields: {named}. "
                "This is a completeness gap, not an accusation."
            ),
            supporting_evidence_ids=[item.id for item, _ in gaps],
            claim_ids=[claim.id],
            event_ids=[],
            affected_system_ids=[claim.claimant_system_id],
        )
