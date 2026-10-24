"""Failure-safe LLM critic (SPEC §13; Phase 4 interface, Phase 6 transport).

This module implements the LLM critic *interface*, strict structured-output validation, and
failure handling. The provider transport is injected: the synchronous implementation lives in
:mod:`nyx.critics.llm_transport`, so the same critic is used by the tests and by the wired
provider without a second interface.

The trust rule is not negotiable: a model may **propose** a finding, but it never decides a
disposition and never changes a lifecycle state. The critic returns a ``CriticResult`` to the
existing registry/composer path, which validates evidence references, persists the run, and
composes a finding only for a ``finding`` result. Model output is an assessment, never
deterministic verification, and model-emitted confidence is not accepted at all.

Fail-closed behaviour:

* no transport configured -> ``ERROR`` and no finding;
* timeout, transport failure, invalid JSON, schema violation, refusal, an unknown/unlinked
  evidence id, or a finding the composer would reject -> ``ERROR``/``INCONCLUSIVE`` and **no
  finding is composed and no lifecycle state changes**;
* only a valid structured assessment that cites linked evidence can become a finding.

The prompt is built with :func:`nyx.ingestion.injection.build_model_prompt`, so evidence is
fenced and labelled as untrusted data rather than concatenated into the prompt by hand.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

import jsonschema  # type: ignore[import-untyped]
from pydantic import Field, StringConstraints
from pydantic import ValidationError as PydanticValidationError

from nyx.critics.base import CriticResult, CriticScope
from nyx.critics.context import CriticContext
from nyx.domain.base import Identifier, NyxModel, utc_now
from nyx.domain.enums import (
    CriticResultStatus,
    CriticType,
    FindingCategory,
    Severity,
)
from nyx.ingestion.injection import build_model_prompt

CRITIC_ID = "c011-llm-critic"
CRITIC_VERSION = "1.0.0"

#: Bumped whenever the prompt text changes, so a stored run says which template produced it.
PROMPT_TEMPLATE_VERSION = "1.0.0"

#: The transport enforces this; the critic does not run its own timer.
DEFAULT_TIMEOUT_SECONDS = 30.0

_ShortText = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
_LimitedText = Annotated[str, StringConstraints(min_length=1, max_length=500)]
_LongText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class LlmTransportError(Exception):
    """The LLM transport could not return a usable response."""


class LlmTimeoutError(LlmTransportError):
    """The LLM transport exceeded its deadline."""


class LlmInvalidOutputError(LlmTransportError):
    """The LLM returned output that is not valid JSON or not valid structured output."""


@runtime_checkable
class LlmTransport(Protocol):
    """The provider seam. Phase 6's implementation is ``SyncCriticTransport``."""

    def complete(self, *, prompt: str, schema: dict[str, Any], timeout_seconds: float) -> str:
        """Return the model's raw response for ``prompt`` within ``timeout_seconds``."""
        ...


#: The strict output contract advertised to the transport. A response that does not validate
#: against this schema is rejected before NYX interprets a single field.
OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "NYX LLM critic structured assessment",
    "type": "object",
    "additionalProperties": False,
    "required": ["result", "proposition", "rationale", "recommended_next_step", "observations"],
    "properties": {
        "result": {
            "type": "string",
            "enum": ["finding", "no_finding", "inconclusive"],
        },
        "proposition": {"type": "string", "minLength": 1, "maxLength": 2000},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 2000},
        "recommended_next_step": {"type": "string", "minLength": 1, "maxLength": 2000},
        "observations": {
            "type": "array",
            "maxItems": 64,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "supporting_evidence_ids": {
            "type": "array",
            "maxItems": 256,
            "items": {"$ref": "#/$defs/evidence_id"},
        },
        "counterevidence_ids": {
            "type": "array",
            "maxItems": 256,
            "items": {"$ref": "#/$defs/evidence_id"},
        },
        "limitations": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
        "inferences": {
            "type": "array",
            "maxItems": 64,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "premises": {
            "type": "array",
            "maxItems": 64,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "recommendations": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "unknowns": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
        "category": {
            "type": "string",
            "enum": [category.value for category in FindingCategory],
        },
        "severity": {
            "type": "string",
            "enum": [severity.value for severity in Severity],
        },
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
        "contradiction": {"type": "string", "minLength": 1, "maxLength": 2000},
        "requires_human_review": {"type": "boolean"},
        "refusal": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    "$defs": {
        "evidence_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        }
    },
}


class _LlmAssessment(NyxModel):
    """The typed form of a schema-valid response.

    ``confidence`` is deliberately absent: SPEC §13 forbids treating a model-emitted confidence
    as a calibrated probability, so the field is rejected as an additional property rather than
    silently imported into a finding.
    """

    result: Literal["finding", "no_finding", "inconclusive"]
    proposition: _LongText
    rationale: _LongText
    recommended_next_step: _LongText
    observations: list[_ShortText] = Field(default_factory=list, max_length=64)
    supporting_evidence_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    counterevidence_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    limitations: list[_LimitedText] = Field(default_factory=list, max_length=32)
    inferences: list[_ShortText] = Field(default_factory=list, max_length=64)
    premises: list[_ShortText] = Field(default_factory=list, max_length=64)
    recommendations: list[_ShortText] = Field(default_factory=list, max_length=32)
    unknowns: list[_LimitedText] = Field(default_factory=list, max_length=32)
    category: FindingCategory | None = None
    severity: Severity | None = None
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4000)] | None = None
    contradiction: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    requires_human_review: bool = False
    refusal: _LongText | None = None


_STATUS = {
    "finding": CriticResultStatus.FINDING,
    "no_finding": CriticResultStatus.NO_FINDING,
    "inconclusive": CriticResultStatus.INCONCLUSIVE,
}


class LlmCritic:
    """An LLM critic behind the deterministic critic interface.

    ``transport=None`` is the supported no-LLM mode: it returns ``ERROR`` and composes nothing.
    A caller that wants a model assessment injects a transport; Phase 6 owns the real one.
    """

    critic_id = CRITIC_ID
    critic_version = CRITIC_VERSION
    description = (
        "Assess linked evidence through an injected LLM transport, validate the structured "
        "output against a strict schema, and propose a finding only when the output cites "
        "linked evidence. Model output is an assessment, never a disposition or a lifecycle "
        "decision."
    )
    scope = CriticScope(
        can_assess=[
            "evidence linked to the event or claim, quoted to the model as untrusted data",
            "proposed findings whose evidence ids resolve to that linked evidence",
        ],
        cannot_assess=[
            "whether a model statement is true; a model assessment is not verification",
            "any evidence id the model cites that NYX has not linked to this context",
            "dispositions, lifecycle transitions, or any external action",
        ],
    )
    critic_type = CriticType.LLM

    def __init__(
        self,
        *,
        transport: LlmTransport | None,
        schema: dict[str, Any],
        clock: Callable[[], datetime] = utc_now,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        model_provider: str | None = None,
        model_version: str | None = None,
        log_prompts: bool = False,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        jsonschema.Draft202012Validator.check_schema(schema)
        self._transport = transport
        self._schema = schema
        self._clock = clock
        self._timeout = timeout_seconds
        self.model_provider = model_provider
        self.model_version = model_version
        self.log_prompts = log_prompts

    def run(self, context: CriticContext) -> CriticResult:
        """Assess ``context`` and return a typed result; never compose or mutate state."""

        if self._transport is None:
            return _failure_result(
                context,
                status=CriticResultStatus.ERROR,
                rationale=(
                    "No LLM transport is configured, so no model assessment was attempted and "
                    "no finding was composed."
                ),
            )

        prompt = self._build_prompt(context)

        def fail(
            status: CriticResultStatus,
            rationale: str,
            *,
            response: str | None = None,
        ) -> CriticResult:
            return _failure_result(
                context,
                status=status,
                rationale=rationale,
                model_provider=self.model_provider,
                model_version=self.model_version,
                prompt_version=PROMPT_TEMPLATE_VERSION,
                llm_io=self._llm_io(prompt, response),
            )

        try:
            raw = self._transport.complete(
                prompt=prompt,
                schema=self._schema,
                timeout_seconds=self._timeout,
            )
        except LlmTimeoutError:
            return fail(
                CriticResultStatus.ERROR,
                "The LLM transport timed out. No finding was composed and no stored state "
                "was changed.",
            )
        except LlmTransportError as exc:
            return fail(
                CriticResultStatus.ERROR,
                f"The LLM transport failed ({type(exc).__name__}). No finding was composed "
                "and no stored state was changed.",
            )
        except Exception as exc:  # noqa: BLE001 - a transport failure must never escape as state
            return fail(
                CriticResultStatus.ERROR,
                f"The LLM transport raised {type(exc).__name__}. No finding was composed and "
                "no stored state was changed.",
            )

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return fail(
                CriticResultStatus.ERROR,
                "The LLM response was not valid JSON. No finding was composed and no stored "
                "state was changed.",
                response=raw if isinstance(raw, str) else None,
            )
        if not isinstance(payload, dict):
            return fail(
                CriticResultStatus.ERROR,
                "The LLM response was not a JSON object. No finding was composed and no "
                "stored state was changed.",
                response=raw,
            )

        try:
            jsonschema.Draft202012Validator(self._schema).validate(payload)
        except jsonschema.exceptions.ValidationError:
            return fail(
                CriticResultStatus.ERROR,
                "The LLM response did not conform to the required output schema. No finding "
                "was composed and no stored state was changed.",
                response=raw,
            )

        try:
            assessment = _LlmAssessment.model_validate(payload)
        except PydanticValidationError:
            return fail(
                CriticResultStatus.ERROR,
                "The LLM response could not be parsed into a critic assessment. No finding "
                "was composed and no stored state was changed.",
                response=raw,
            )

        if assessment.refusal is not None:
            return fail(
                CriticResultStatus.INCONCLUSIVE,
                "The model declined to assess the evidence. A refusal is not evidence of "
                "misconduct, and no finding was composed.",
                response=raw,
            )

        known_ids = {item.id for item in context.linked_evidence}
        referenced = [*assessment.supporting_evidence_ids, *assessment.counterevidence_ids]
        unknown_ids = sorted(
            {evidence_id for evidence_id in referenced if evidence_id not in known_ids}
        )
        if unknown_ids:
            return fail(
                CriticResultStatus.INCONCLUSIVE,
                "The model cited evidence IDs that are not linked to this context "
                f"({', '.join(unknown_ids)}). NYX cannot validate an assessment about evidence "
                "it has not seen, so no finding was composed.",
                response=raw,
            )

        status = _STATUS[assessment.result]
        if status is CriticResultStatus.FINDING:
            rejection = _finding_rejection(assessment, referenced, context)
            if rejection is not None:
                return fail(
                    CriticResultStatus.ERROR,
                    f"{rejection} No finding was composed and no stored state was changed.",
                    response=raw,
                )

        return CriticResult(
            critic_id=CRITIC_ID,
            critic_version=CRITIC_VERSION,
            result=status,
            proposition=assessment.proposition,
            scope=context.scope,
            observations=list(assessment.observations),
            supporting_evidence_ids=list(assessment.supporting_evidence_ids),
            counterevidence_ids=list(assessment.counterevidence_ids),
            limitations=list(assessment.limitations),
            rationale=assessment.rationale,
            recommended_next_step=assessment.recommended_next_step,
            requires_human_review=(
                True if status is CriticResultStatus.FINDING else assessment.requires_human_review
            ),
            category=assessment.category,
            severity=assessment.severity,
            title=assessment.title,
            summary=assessment.summary,
            inferences=list(assessment.inferences),
            premises=list(assessment.premises),
            contradiction=assessment.contradiction,
            recommendations=list(assessment.recommendations),
            unknowns=list(assessment.unknowns),
            affected_system_ids=[context.scope.system_id],
            claim_ids=[context.linked_claim.id] if context.linked_claim else [],
            event_ids=[context.target_event.id],
            model_provider=self.model_provider,
            model_version=self.model_version,
            prompt_version=PROMPT_TEMPLATE_VERSION,
            resource_usage=self._usage(),
            llm_io=self._llm_io(prompt, raw),
        )

    def _usage(self) -> dict[str, float] | None:
        """Return the transport's token accounting as optional run metadata.

        Usage is best-effort: a transport that reports none yields ``None`` rather than zeros,
        because "we did not measure" and "it cost nothing" are different statements.
        """

        usage = getattr(self._transport, "last_usage", None)
        if not isinstance(usage, dict):
            return None
        measured = {
            key: float(value)
            for key, value in usage.items()
            if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
        }
        return measured or None

    def _llm_io(self, prompt: str, response: str | None) -> dict[str, Any] | None:
        """Return the prompt/response pair only when prompt logging is explicitly enabled."""

        if not self.log_prompts:
            return None
        payload: dict[str, Any] = {"prompt": prompt}
        if response is not None:
            payload["response"] = response
        return payload

    def _build_prompt(self, context: CriticContext) -> str:
        question = "\n".join(
            (
                f"Assess event {context.target_event.id} using only the evidence quoted below.",
                f"Assessed as of {self._clock().isoformat()}.",
                f"Prompt template version: {PROMPT_TEMPLATE_VERSION}.",
                "Return JSON only, conforming exactly to this JSON Schema:",
                json.dumps(self._schema, indent=2, sort_keys=True),
                "Cite only evidence IDs that appear in the quoted evidence. Do not decide a "
                "disposition and do not claim to have changed any lifecycle state.",
            )
        )
        return build_model_prompt(question=question, evidence=context.linked_evidence)


def _finding_rejection(
    assessment: _LlmAssessment, referenced: list[str], context: CriticContext
) -> str | None:
    """Return why a proposed finding is not safe to compose, or ``None`` when it is.

    These are the composer preconditions that would otherwise raise after the run row is added.
    Checking them here keeps a malformed model proposal from turning into an exception or an
    unusable half-written run.
    """

    if not referenced:
        return "The model proposed a finding without citing any linked evidence."
    if not context.scope.has_detail():
        return "The assessment scope does not narrow beyond a bare system id."
    if not assessment.observations:
        return "The model proposed a finding without recording any observation."
    if assessment.category is FindingCategory.CONTRADICTION and not assessment.contradiction:
        return "The model proposed a contradiction finding without stating the contradiction."
    return None


def _failure_result(
    context: CriticContext,
    *,
    status: CriticResultStatus,
    rationale: str,
    model_provider: str | None = None,
    model_version: str | None = None,
    prompt_version: str | None = None,
    llm_io: dict[str, Any] | None = None,
) -> CriticResult:
    """Build an ``ERROR``/``INCONCLUSIVE`` result that composes no finding."""

    return CriticResult(
        critic_id=CRITIC_ID,
        critic_version=CRITIC_VERSION,
        result=status,
        proposition=f"{CRITIC_ID} did not produce a composable assessment",
        scope=context.scope,
        observations=[
            "The LLM assessment path failed closed; no finding was composed and no stored "
            "finding state was changed."
        ],
        rationale=rationale,
        recommended_next_step=(
            "Inspect the critic run rationale and retry. Do not treat the absence of a finding "
            "as evidence that the underlying claim is true."
        ),
        requires_human_review=True,
        model_provider=model_provider,
        model_version=model_version,
        prompt_version=prompt_version,
        llm_io=llm_io,
    )
