"""The shared lifecycle for every agent-transcript adapter (ADR 0010).

An agent-transcript adapter observes one agent's own conversation log — Hermes, OpenCode, and
the others — and hands NYX the same producer *input* models a human would submit. It is never a
new ingestion path: its output goes through the ordinary gateway, so it inherits redaction,
schema validation, identity deduplication, and per-record audit for free (the same boundary the
git-ci adapter uses, ``src/nyx/adapters/base.py`` → ``nyx.ingestion.inputs`` →
``IngestionGateway``).

Every supported agent is the **same shape** behind this base class: an append-only, typed event
stream in one database, keyed by (session, ordinal), where assistant text is a candidate claim
and a tool call plus its result is candidate evidence. This base class implements everything
that shape has in common — the read-only health probe, the stable identities that make a
re-run a no-op, and the mapping of one normalized turn onto the producer input models — and
leaves exactly two hooks to the subclass:

* :meth:`iter_turns` — read the store and yield :class:`Turn` values;
* :meth:`default_db_path` — where the store lives when no path is configured.

Three behaviours are the point of this module, and each is easy to get wrong:

* **It is read-only on the source.** The store is opened with a ``mode=ro`` URI and nothing is
  ever written to it (ADR 0004). NYX can never corrupt -- or even touch -- the agent's live
  database.
* **It is stateless.** There is no cursor and no session. Idempotency comes from *stable
  identities*: every emitted event, claim, and evidence item carries a deterministic ``id`` and
  ``producer_event_id`` derived from the transcript's own ids. The ingestion gateway deduplicates
  on ``producer_event_id`` against a hash of the whole payload, so a second fetch of the same
  records is a set of duplicates, not new rows -- which is what makes a timer safe.
* **An error is not an empty result.** A missing store raises :class:`TranscriptError`, which
  propagates out of :meth:`fetch_for_ref`; "the adapter is down" is an error a caller must
  retry, not an observation that the agent said nothing.

This module imports only ``nyx.adapters``, ``nyx.domain``, ``nyx.ingestion``, and the standard
library. It imports nothing from ``nyx.critics``, ``nyx.findings``, ``nyx.disputes``,
``nyx.policies``, ``nyx.storage``, ``nyx.retention``, ``nyx.auth``, ``nyx.systems``, or
``nyx.audit``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nyx.adapters.agent_transcript.config import AgentTranscriptConfig
from nyx.adapters.agent_transcript.errors import NormalizationError, TranscriptError
from nyx.adapters.base import AdapterHealth, AdapterInput, AdapterState
from nyx.config import Settings
from nyx.domain.base import PrincipalRef, Scope, UtcDatetime, utc_now
from nyx.domain.enums import (
    ClaimType,
    EvidenceKind,
    IntegrityMethod,
    PrincipalKind,
    RetentionClass,
    Sensitivity,
    TrustClass,
)
from nyx.domain.events import EventType
from nyx.ingestion.inputs import ClaimInput, EventInput, EvidenceInput

#: The event type stamped on every turn. ``agent.turn`` is in NYX's own vocabulary, not any
#: agent's, so an observed turn cannot be confused with a native event of another system.
_EVENT_TYPE: EventType = "agent.turn"

#: A claim shorter than this (after cleaning) is almost never an assessable proposition -- it is
#: chatter ("ok", "let me check") -- so it is recorded as an event but not filed as a claim.
#: One tunable constant keeps the filter honest and testable rather than scattered as magic
#: numbers through the normalizer.
MIN_STATEMENT_LENGTH = 12

#: The access policy every piece of transcript-derived evidence is stamped with. It is a
#: reference string, not a policy object, so the adapter never couples to NYX's policy store.
_EVIDENCE_ACCESS_POLICY_REF = "policy:evidence-internal-read"

#: Evidence is internal by default: it is one agent's own actions, seen by an observer that was
#: not the actor, so it is never more public than ``internal`` and never less trustworthy than
#: ``observed``.
_EVIDENCE_SENSITIVITY = Sensitivity.INTERNAL
_EVENT_TRUST_CLASS = TrustClass.OBSERVED


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation and its result, as read from the transcript.

    ``result`` is the full, untruncated output text so the evidence ``content_hash`` covers the
    whole artifact; the emitted description is truncated separately. ``arguments`` is the parsed
    argument object (never the raw string), so a subclass can summarize it redaction-safely.
    """

    call_id: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: str = ""


@dataclass(frozen=True)
class Turn:
    """One assistant turn reduced to the fields the shared mapper needs.

    A subclass turns the agent's native record into a :class:`Turn`; this base class turns the
    :class:`Turn` into producer inputs. That split is the whole template: the two hooks a new
    agent needs are "how do I read a record" and "how do I fill these fields", nothing more.
    """

    turn_id: str
    session_id: str
    occurred_at: datetime
    statement: str = ""
    workspace: str | None = None
    branch: str | None = None
    calls: tuple[ToolCall, ...] = ()


def _sha256_digest(text: str) -> str:
    """Return ``sha256:<hex>`` of ``text`` -- the ``Digest`` the evidence integrity field wants."""

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_statement(text: str) -> str:
    """Collapse whitespace and trim a candidate claim to a single normalized line."""

    return " ".join((text or "").split()).strip()


class AgentTranscriptAdapter:
    """Shared base for adapters that read an agent's local conversation-log database.

    Subclasses set the class attributes (``adapter_id`` et al.) and implement
    :meth:`iter_turns` and :meth:`default_db_path`. Everything else -- health, the fetch loop,
    the idempotent identities, and the mapping onto :class:`EventInput` / :class:`ClaimInput` /
    :class:`EvidenceInput` -- lives here and is identical for every agent.
    """

    adapter_id: str = "agent-transcript"
    version = "1.0.0"
    description = (
        "Observes an agent's own conversation log and emits its assistant turns as NYX events, "
        "its statements as claims, and its tool results as evidence. Read-only: it opens the "
        "transcript store read-only and emits producer inputs, never findings, dispositions, "
        "or claims, and never writes to the agent's database."
    )
    permissions = ["transcript.read"]
    can_observe = [
        "the assistant turns an agent recorded in its own conversation log",
        "the statements an agent made in those turns",
        "the tool calls an agent made and the results it observed",
    ]
    cannot_observe = [
        "the truth of a claim; a transcript records that the agent said it, not that it is so",
        "records from any agent other than the one this adapter was built for",
        "anything the agent did not itself record -- the log is the only source",
        "a git ref: a conversation is not scoped to a branch, so the watcher's ref is ignored",
    ]

    def __init__(self, config: AgentTranscriptConfig) -> None:
        self._config = config

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentTranscriptAdapter:
        """Construct the adapter from the shared ``agent_transcript_*`` settings."""

        return cls(AgentTranscriptConfig.from_settings(settings, default_db=cls.default_db_path()))

    @classmethod
    def default_db_path(cls) -> Path:
        """The store's location when ``NYX_AGENT_TRANSCRIPT_DB`` is not set.

        A subclass overrides this with its own auto-detect path. The base raises rather than
        guess a path, because a wrong default would make the adapter silently read nothing.
        """

        raise NotImplementedError

    # -- Adapter protocol --------------------------------------------------------------

    def health(self) -> AdapterHealth:
        """Probe the store. Never raises: a missing or unreadable store is ``DEGRADED``."""

        checked_at = utc_now()
        try:
            self._connect()
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            return AdapterHealth(
                adapter_id=self.adapter_id,
                version=self.version,
                state=AdapterState.DEGRADED,
                detail=(
                    f"{self.adapter_id} transcript store is not observable: {type(exc).__name__}"
                ),
                checked_at=checked_at,
            )
        return AdapterHealth(
            adapter_id=self.adapter_id,
            version=self.version,
            state=AdapterState.OK,
            detail=f"{self.adapter_id} transcript store opened read-only at {self._config.db_path}",
            checked_at=checked_at,
        )

    def normalize(self, raw: object) -> list[AdapterInput]:
        """Turn one normalized :class:`Turn` into its producer inputs.

        Emits one event per turn, one claim when the turn carries a substantive statement, and
        one evidence item per tool call. The event payload carries the claim and evidence ids so
        the findings service can link them without a second round trip -- the gateway assigns no
        id the adapter does not already know, because the adapter supplies stable ids.
        """

        if not isinstance(raw, Turn):
            raise NormalizationError("expected a Turn", field="raw")

        turn = raw
        system_id = self._config.system_id
        event_id = f"{system_id}:evt:{turn.turn_id}"
        claim_id = f"{system_id}:clm:{turn.turn_id}"

        # Build each input once. The claim and evidence ids are known up front (they are derived
        # from the transcript's own ids), so the event payload can reference real ids without a
        # second round trip -- the gateway assigns no id the adapter does not already supply.
        statement = _clean_statement(turn.statement)
        claim = (
            self._claim(turn, event_id, claim_id, statement=statement)
            if len(statement) >= MIN_STATEMENT_LENGTH
            else None
        )
        evidence = [self._evidence(turn, call, claim_id if claim else None) for call in turn.calls]
        evidence_ids = [item.id for item in evidence if item.id is not None]

        inputs: list[AdapterInput] = [
            self._event(turn, event_id, claim.id if claim else None, evidence_ids)
        ]
        if claim is not None:
            inputs.append(claim)
        inputs.extend(evidence)
        return inputs

    def fetch_for_ref(self, ref: str) -> list[AdapterInput]:
        """Read the whole store and return every turn's producer inputs.

        ``ref`` is accepted to satisfy the ``Adapter`` protocol but is deliberately **not
        applied**: a conversation log is not scoped to a git branch, so the git ref the watcher
        passes (``NYX_WATCH_REF``, "main" by default) is meaningless here and is ignored. The
        honest reading of an empty result is therefore "the store held no turns", never "the
        ref had nothing".

        A :class:`TranscriptError` from a missing or unreadable store **propagates**: "the
        adapter is down" is an error a caller must retry, not an empty observation set.
        """

        if not isinstance(ref, str):
            raise NormalizationError("ref must be a string", field="ref")
        if not self._config.db_path.exists():
            raise TranscriptError(
                f"the {self.adapter_id} transcript store is not at {self._config.db_path}; "
                "set NYX_AGENT_TRANSCRIPT_DB to its path or do not enable the adapter",
                code="transcript_unavailable",
            )

        inputs: list[AdapterInput] = []
        for turn in self.iter_turns():
            inputs.extend(self.normalize(turn))
        return inputs

    # -- subclass hooks ---------------------------------------------------------------

    def iter_turns(self) -> Iterator[Turn]:
        """Read the store and yield one :class:`Turn` per assistant turn.

        Implemented by subclasses; the base raises so an unimplemented adapter cannot emit an
        empty observation that reads as "the agent said nothing".
        """

        raise NotImplementedError

    # -- internals: the shared turn -> inputs mapper ----------------------------------

    def _connect(self) -> None:
        """Open the store read-only and run a liveness query. Never writes; never creates."""

        import sqlite3

        uri = f"file:{self._config.db_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            connection.execute("SELECT 1")
        finally:
            connection.close()

    def _event(
        self,
        turn: Turn,
        event_id: str,
        claim_id: str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> EventInput:
        payload: dict[str, Any] = {
            "turn_id": turn.turn_id,
            "session_id": turn.session_id,
            "tool_count": len(turn.calls),
            "has_statement": bool(_clean_statement(turn.statement)),
        }
        if turn.workspace:
            payload["workspace"] = turn.workspace
        if turn.branch:
            payload["branch"] = turn.branch
        if claim_id is not None:
            payload["claim_id"] = claim_id
        if evidence_ids:
            payload["evidence_ids"] = evidence_ids
        return EventInput(
            id=event_id,
            source_system_id=self._config.system_id,
            event_type=_EVENT_TYPE,
            occurred_at=_as_utc(turn.occurred_at),
            correlation_id=f"{self._config.system_id}:{turn.turn_id}",
            actor=self._agent_ref(),
            subject=self._agent_ref(),
            payload=payload,
            source_ref=f"{self.adapter_id}://{turn.session_id}/{turn.turn_id}",
            trust_class=_EVENT_TRUST_CLASS,
            labels=["source:transcript", self.adapter_id],
            retention_class=RetentionClass.STANDARD,
            producer_event_id=f"{self._config.system_id}:pevt:{turn.turn_id}",
        )

    def _claim(
        self,
        turn: Turn,
        event_id: str,
        claim_id: str,
        *,
        statement: str | None = None,
    ) -> ClaimInput:
        cleaned = _clean_statement(statement or turn.statement)
        conditions = [f"session:{turn.session_id}", f"turn:{turn.turn_id}"]
        if turn.workspace:
            conditions.append(f"cwd:{turn.workspace}")
        if turn.branch:
            conditions.append(f"branch:{turn.branch}")
        return ClaimInput(
            id=claim_id,
            statement=cleaned[:2000],
            claimant_system_id=self._config.system_id,
            claim_type=ClaimType.FACTUAL,
            scope=Scope(
                system_id=self._config.system_id,
                artifact=self.adapter_id,
                environment="local",
                conditions=conditions[:32],
            ),
            created_at=_as_utc(turn.occurred_at),
            source_event_ids=[event_id],
            retention_class=RetentionClass.STANDARD,
            producer_event_id=f"{self._config.system_id}:pclm:{turn.turn_id}",
        )

    def _evidence(
        self,
        turn: Turn,
        call: ToolCall,
        claim_id: str | None,
    ) -> EvidenceInput:
        result = call.result or ""
        description = (
            f"Agent invoked `{call.tool}`"
            + (f" with arguments {self._summarize_args(call.arguments)}" if call.arguments else "")
            + ". Result (truncated):\n"
            + (result[:1800] if result else "(no result text)")
        )
        return EvidenceInput(
            id=f"{self._config.system_id}:ev:{call.call_id}",
            kind=self._evidence_kind(call.tool),
            title=f"Tool call: {call.tool}",
            description=description[:2000],
            source_system_id=self._config.system_id,
            source_name=self.adapter_id,
            source_ref=f"{self.adapter_id}://tool/{call.call_id}",
            # A locator *into the transcript* -- where this evidence came from -- so the record is
            # re-auditable. It is a reference string, not a filesystem path, so the path-traversal
            # guard never mistakes it for an escape attempt.
            locator=f"transcript:{turn.session_id}#turn:{turn.turn_id}#call:{call.call_id}",
            scope=Scope(
                system_id=self._config.system_id,
                artifact=self.adapter_id,
                environment="local",
            ),
            captured_at=_as_utc(turn.occurred_at),
            valid_from=_as_utc(turn.occurred_at),
            content_hash=_sha256_digest(result),
            integrity_method=IntegrityMethod.HASH,
            producer_identity=self._agent_ref(),
            relevance_to_claim=claim_id,
            relevance_note=(
                f"Tool {call.tool} invoked by the agent during the turn; its result is recorded "
                "as evidence of the action, not as independent verification of the claim."
            ),
            limitations=[
                "Tool output is the agent's own action, not an independent verification of the "
                "claim it accompanies.",
            ],
            sensitivity=_EVIDENCE_SENSITIVITY,
            access_policy_ref=_EVIDENCE_ACCESS_POLICY_REF,
            retention_class=RetentionClass.STANDARD,
            producer_event_id=f"{self._config.system_id}:pev:{call.call_id}",
        )

    @staticmethod
    def _summarize_args(arguments: dict[str, Any]) -> str:
        """A compact, length-capped rendering of tool arguments for the evidence description."""

        try:
            rendered = json.dumps(arguments, ensure_ascii=True, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return "<unserializable>"
        return rendered[:300]

    @staticmethod
    def _evidence_kind(tool: str) -> EvidenceKind:
        """Map a tool name to an evidence kind. Unknown tools fall back to a runtime observation."""

        table = {
            "terminal": EvidenceKind.MEASUREMENT,
            "shell": EvidenceKind.MEASUREMENT,
            "bash": EvidenceKind.MEASUREMENT,
            "web_search": EvidenceKind.EXTERNAL_REFERENCE,
            "web_fetch": EvidenceKind.EXTERNAL_REFERENCE,
            "read_file": EvidenceKind.SOURCE_FILE,
            "view": EvidenceKind.SOURCE_FILE,
            "write_file": EvidenceKind.SOURCE_FILE,
            "edit": EvidenceKind.SOURCE_FILE,
            "patch": EvidenceKind.SOURCE_FILE,
            "apply_patch": EvidenceKind.SOURCE_FILE,
            "ls": EvidenceKind.LOG,
            "glob": EvidenceKind.LOG,
            "grep": EvidenceKind.LOG,
            "list_dir": EvidenceKind.LOG,
            "search_files": EvidenceKind.LOG,
        }
        return table.get(tool, EvidenceKind.RUNTIME_OBSERVATION)

    def _agent_ref(self) -> PrincipalRef:
        return PrincipalRef(
            id=self._config.system_id,
            kind=PrincipalKind.AGENT,
            display_name=self.adapter_id,
        )


__all__ = [
    "AgentTranscriptAdapter",
    "MIN_STATEMENT_LENGTH",
    "ToolCall",
    "Turn",
]


def _as_utc(value: datetime) -> UtcDatetime:
    """Normalise a datetime to an aware UTC value the ``UtcDatetime`` field accepts."""

    if value.tzinfo is None:
        # The transcript stores naive epoch seconds; interpret a naive value as UTC, never local.
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
