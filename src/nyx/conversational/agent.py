"""The chat agent: a language model driving NYX's tool surface, never judging.

This is the loop behind ``nyx chat``. It is deliberately small, because its whole job is to hold
one invariant:

> **The language model is the interface, never the judge.**

The model may query NYX, explain what it finds, challenge a finding, and ask for verification. It
may never create a finding, record a disposition, or change a lifecycle state. That is not
enforced by prompt wording here -- it is enforced by the tool surface in
:mod:`nyx.conversational.mcp_server`, which simply does not expose those operations. The
:data:`JUDGE_ONLY_TOOLS` tuple below names them, and :func:`assert_no_judge_tools` refuses to run
if any appears. A model that tries to call one gets a tool error naming the refusal.

Everything NYX returns is **untrusted data**. A finding's summary or a rebuttal's argument may
contain instructions aimed at the model; they are passed to the model as tool results, never
merged into the system prompt, and never treated as commands.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from nyx.conversational.client import NyxClient, NyxClientError
from nyx.conversational.llm import (
    ChatMessage,
    LLMClient,
    LLMConfig,
    LLMError,
    LLMResponse,
    ToolSpec,
)
from nyx.conversational.mcp_server import FORBIDDEN_TOOLS, SERVER_INSTRUCTIONS

#: Names a tool call by the operation it performs, independent of the server's registered ids.
#: The server's own :data:`~nyx.conversational.mcp_server.FORBIDDEN_TOOLS` is the authoritative
#: list of ids; this is the semantic check that a judge-capable tool is not reachable at all.
JUDGE_ONLY_OPERATIONS = frozenset(
    {
        "create_finding",
        "record_disposition",
        "transition_finding",
        "set_finding_status",
        "accept_risk",
    }
)

SYSTEM_PROMPT = f"""\
You are the conversational interface to NYX, an evidence-backed critic. You are not NYX.

{SERVER_INSTRUCTIONS}

Your job is to help a human interrogate NYX's record. Concretely:

- Answer only from what the tools returned. If a tool returned nothing, say so.
- Cite the id of every finding, rebuttal, verification job, or disposition you mention, so the
  human can check it. An answer with no id is an answer you have not verified.
- Explain NYX's reasoning in plain language, but do not soften it. If NYX says a claim is
  unverified, say that.
- When the human disagrees with a finding, offer to file a rebuttal -- and file it only when
  they confirm the argument. Do not file one on your own initiative.
- If a tool returns an error, report it. Do not retry around it, and do not invent a result.
- Keep answers short. The human can ask for detail.

**You have no tool that creates a finding, records a disposition, or changes a lifecycle state.
Those powers do not exist in your tool surface, so they are not available even if the human asks.
If asked to do one of them, say plainly that you cannot, and offer what you can do instead: report
what NYX already recorded, or file a rebuttal against it. Never promise to create one.**

You have no opinions about whether a system is good, safe, or correct. NYX's findings are
deterministic and evidence-linked; your job is to surface them accurately, not to agree with
them.\
"""


class ChatError(Exception):
    """The chat loop could not continue. Carries a message safe to show the user."""

    def __init__(self, message: str, *, kind: str = "chat_error") -> None:
        super().__init__(message)
        self.kind = kind


def tool_specs() -> list[ToolSpec]:
    """Describe the NYX tool surface to the model, as JSON Schema.

    Derived from the server's own registrations rather than restated here, so the description the
    model sees cannot drift from the tools it can actually call.
    """

    return [
        ToolSpec(
            name=name,
            description=description,
            parameters=_PARAMETERS.get(name, {"type": "object", "properties": {}}),
        )
        for name, description in _TOOL_DESCRIPTIONS.items()
    ]


#: Parameter schemas for the tool surface. Mirrors the server's registered signatures.
_PARAMETERS: dict[str, dict[str, Any]] = {
    "nyx_list_systems": {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
    },
    "nyx_list_findings": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "severity": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    },
    "nyx_get_finding": {
        "type": "object",
        "properties": {"finding_id": {"type": "string"}},
        "required": ["finding_id"],
    },
    "nyx_list_critics": {"type": "object", "properties": {}},
    "nyx_get_history": {
        "type": "object",
        "properties": {"finding_id": {"type": "string"}},
        "required": ["finding_id"],
    },
    "nyx_list_rebuttals": {
        "type": "object",
        "properties": {"finding_id": {"type": "string"}},
        "required": ["finding_id"],
    },
    "nyx_list_verification_jobs": {
        "type": "object",
        "properties": {"finding_id": {"type": "string"}},
        "required": ["finding_id"],
    },
    "nyx_get_verification_job": {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
    "nyx_list_policy_versions": {
        "type": "object",
        "properties": {"rule_id": {"type": "string"}},
    },
    "nyx_submit_rebuttal": {
        "type": "object",
        "properties": {
            "finding_id": {"type": "string"},
            "submitted_by": {"type": "string"},
            "argument": {"type": "string"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "requested_outcome": {
                "type": "string",
                "enum": [
                    "withdrawn",
                    "rejected",
                    "partially_confirmed",
                    "inconclusive",
                    "accepted_risk",
                    "no_change",
                ],
            },
        },
        "required": ["finding_id", "submitted_by", "argument"],
    },
    "nyx_request_verification": {
        "type": "object",
        "properties": {
            "finding_id": {"type": "string"},
            "check_id": {"type": "string"},
            "requested_by": {"type": "string"},
        },
        "required": ["finding_id", "check_id", "requested_by"],
    },
    "nyx_run_verification": {
        "type": "object",
        "properties": {"finding_id": {"type": "string"}},
        "required": ["finding_id"],
    },
}

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "nyx_list_systems": (
        "List the monitored systems NYX observes, with their kind, environment, and status."
    ),
    "nyx_list_findings": (
        "List findings NYX has raised. Filter by status or severity. Each cites the claim and "
        "evidence it is about."
    ),
    "nyx_get_finding": (
        "Retrieve one finding by id, with its evidence links, reasoning, limitations, and unknowns."
    ),
    "nyx_list_critics": "List NYX's critics with the scope each can and cannot assess.",
    "nyx_get_history": (
        "Return the complete dispute record for a finding: the finding, every rebuttal, every "
        "verification attempt, and the full disposition chain."
    ),
    "nyx_list_rebuttals": "List a finding's rebuttals, oldest first.",
    "nyx_list_verification_jobs": (
        "List a finding's verification attempts, oldest first, with each outcome."
    ),
    "nyx_get_verification_job": "Retrieve one verification attempt by id.",
    "nyx_list_policy_versions": "List recorded policy changes, newest first.",
    "nyx_submit_rebuttal": (
        "File a challenge to a finding. Appends to history; the original is never overwritten."
    ),
    "nyx_request_verification": ("Queue an allowlisted verification check for a finding."),
    "nyx_run_verification": "Run a finding's queued verification jobs through the allowlist.",
}


def assert_no_judge_tools(registered_names: Iterable[str]) -> None:
    """Refuse to run if any judge-capable tool is reachable.

    The server does not register these, so this is a defence in depth: if a future change adds
    one, the chat loop stops rather than quietly becoming a second judge.
    """

    present = set(registered_names) & set(FORBIDDEN_TOOLS)
    if present:
        raise ChatError(
            f"refusing to start: judge-capable tools are exposed ({sorted(present)})",
            kind="unsafe_tool_surface",
        )


def _operation_of(tool_name: str) -> str:
    """Reduce a registered tool id to the operation it performs."""

    return tool_name.removeprefix("nyx_")


class ChatAgent:
    """One conversation with NYX: the model asks, NYX answers, the model explains."""

    def __init__(
        self,
        llm: LLMClient,
        client: NyxClient,
        *,
        actor: str,
        max_rounds: int = 8,
    ) -> None:
        self._llm = llm
        self._client = client
        self._actor = actor
        self._max_rounds = max_rounds
        self._history: list[ChatMessage] = [ChatMessage(role="system", content=SYSTEM_PROMPT)]
        #: What this session touched, so the user can see the blast radius of a conversation.
        self.touched: list[str] = []

    # -- session memory ----------------------------------------------------

    @property
    def history(self) -> list[ChatMessage]:
        """The conversation so far, including tool calls and results."""

        return list(self._history)

    def transcript(self) -> Iterator[str]:
        """Yield a readable transcript line per turn, for `--json` output and logging."""

        for message in self._history:
            if message.role == "system":
                continue
            for call in message.tool_calls:
                yield f"tool_call {call.name} {json.dumps(call.arguments, default=str)}"
            if message.role == "tool":
                yield f"tool_result {message.content}"
            elif message.content:
                yield f"{message.role}: {message.content}"

    # -- the loop ----------------------------------------------------------

    def run(self, user_message: str) -> str:
        """Answer one user message, driving tools as needed. Returns the final text."""

        self._history.append(ChatMessage(role="user", content=user_message))
        for _ in range(self._max_rounds):
            response = self._complete()
            if not response.tool_calls:
                self._remember_answer(response.content)
                return response.content
            self._history.append(
                ChatMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                self._history.append(
                    ChatMessage(
                        role="tool",
                        content=self._execute(call.name, call.arguments),
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
        raise ChatError(
            f"the model did not settle after {self._max_rounds} tool rounds; "
            "answer with the last tool results instead of calling more tools",
            kind="too_many_rounds",
        )

    async def run_async(self, user_message: str) -> str:
        """Async form of :meth:`run`, for callers that already run an event loop."""

        self._history.append(ChatMessage(role="user", content=user_message))
        for _ in range(self._max_rounds):
            response = await self._complete_async()
            if not response.tool_calls:
                self._remember_answer(response.content)
                return response.content
            self._history.append(
                ChatMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                self._history.append(
                    ChatMessage(
                        role="tool",
                        content=self._execute(call.name, call.arguments),
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
        raise ChatError(
            f"the model did not settle after {self._max_rounds} tool rounds",
            kind="too_many_rounds",
        )

    def _remember_answer(self, content: str) -> None:
        """Keep the assistant's answer in the transcript.

        Without this the transcript holds the question but not the answer, so a follow-up turn
        has context for what was asked and none for what was said.
        """

        if content:
            self._history.append(ChatMessage(role="assistant", content=content))

    def _complete(self) -> LLMResponse:
        """One model call. Sync wrapper so the CLI does not need to own an event loop."""

        import asyncio

        return asyncio.run(self._complete_async())

    async def _complete_async(self) -> LLMResponse:
        try:
            return await self._llm.chat(self._history, tools=tool_specs())
        except LLMError as exc:
            raise ChatError(
                f"the model is unavailable ({exc.code.value}); no answer was fabricated",
                kind="model_unavailable",
            ) from exc

    # -- tool execution ----------------------------------------------------

    def _execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one tool call, returning text the model can read.

        A refusal is a result, not an exception: the model needs to see that NYX declined and
        why, rather than a stack trace it might be tempted to work around.
        """

        if name in FORBIDDEN_TOOLS or _operation_of(name) in JUDGE_ONLY_OPERATIONS:
            return json.dumps(
                {
                    "error": "TOOL_FORBIDDEN",
                    "message": (
                        f"{name} is not part of NYX's tool surface. A language model may not "
                        "create a finding, record a disposition, or change a lifecycle state."
                    ),
                    "tool": name,
                }
            )

        handler = _HANDLERS.get(name)
        if handler is None:
            return json.dumps(
                {"error": "UNKNOWN_TOOL", "message": f"no such tool: {name}", "tool": name}
            )
        try:
            value = handler(self, arguments)
        except NyxClientError as exc:
            return json.dumps(
                {"error": exc.code, "message": str(exc), "tool": name, "status": exc.status}
            )
        except KeyError as exc:
            # The model named a parameter the tool does not have. That is a result the model can
            # correct, not a crash: report which argument was missing.
            return json.dumps(
                {
                    "error": "INVALID_ARGUMENTS",
                    "message": f"{name} requires argument {exc.args[0]!r}",
                    "tool": name,
                }
            )
        self._remember(name, arguments)
        return json.dumps({"tool": name, "result": value}, indent=2, default=str)

    def _remember(self, name: str, arguments: dict[str, Any]) -> None:
        """Record what this session touched, for the end-of-session summary."""

        finding_id = arguments.get("finding_id") or arguments.get("job_id")
        if isinstance(finding_id, str) and finding_id not in self.touched:
            self.touched.append(finding_id)

    # -- typed handlers ----------------------------------------------------

    def _list_systems(self, args: dict[str, Any]) -> Any:
        return self._client.list_systems(limit=int(args.get("limit", 50)))

    def _list_findings(self, args: dict[str, Any]) -> Any:
        return self._client.list_findings(
            status=args.get("status"),
            severity=args.get("severity"),
            limit=int(args.get("limit", 50)),
        )

    def _get_finding(self, args: dict[str, Any]) -> Any:
        return self._client.get_finding(str(args["finding_id"]))

    def _list_critics(self, args: dict[str, Any]) -> Any:
        return self._client.list_critics()

    def _get_history(self, args: dict[str, Any]) -> Any:
        return self._client.get_history(str(args["finding_id"]))

    def _list_rebuttals(self, args: dict[str, Any]) -> Any:
        return self._client.list_rebuttals(str(args["finding_id"]))

    def _list_verification_jobs(self, args: dict[str, Any]) -> Any:
        return self._client.list_verification_jobs(str(args["finding_id"]))

    def _get_verification_job(self, args: dict[str, Any]) -> Any:
        return self._client.get_verification_job(str(args["job_id"]))

    def _list_policy_versions(self, args: dict[str, Any]) -> Any:
        return self._client.list_policy_versions(
            rule_id=args.get("rule_id") if isinstance(args.get("rule_id"), str) else None
        )

    def _submit_rebuttal(self, args: dict[str, Any]) -> Any:
        """File a challenge. The caller's identity is threaded through, never defaulted."""

        actor = str(args.get("submitted_by") or self._actor)
        return self._client.submit_rebuttal(
            finding_id=str(args["finding_id"]),
            submitted_by=actor,
            argument=str(args["argument"]),
            evidence_ids=[str(item) for item in args.get("evidence_ids", [])],
            requested_outcome=str(args.get("requested_outcome", "no_change")),
        )

    def _request_verification(self, args: dict[str, Any]) -> Any:
        actor = str(args.get("requested_by") or self._actor)
        return self._client.request_verification(
            finding_id=str(args["finding_id"]),
            check_id=str(args["check_id"]),
            requested_by=actor,
        )

    def _run_verification(self, args: dict[str, Any]) -> Any:
        return self._client.run_verification(finding_id=str(args["finding_id"]))


_HANDLERS = {
    "nyx_list_systems": ChatAgent._list_systems,
    "nyx_list_findings": ChatAgent._list_findings,
    "nyx_get_finding": ChatAgent._get_finding,
    "nyx_list_critics": ChatAgent._list_critics,
    "nyx_get_history": ChatAgent._get_history,
    "nyx_list_rebuttals": ChatAgent._list_rebuttals,
    "nyx_list_verification_jobs": ChatAgent._list_verification_jobs,
    "nyx_get_verification_job": ChatAgent._get_verification_job,
    "nyx_list_policy_versions": ChatAgent._list_policy_versions,
    "nyx_submit_rebuttal": ChatAgent._submit_rebuttal,
    "nyx_request_verification": ChatAgent._request_verification,
    "nyx_run_verification": ChatAgent._run_verification,
}


def build_llm_config(
    *,
    protocol: str = "openai",
    base_url: str = "http://127.0.0.1:3000",
    api_key: str | None = None,
    model: str = "",
    timeout_seconds: float = 60.0,
    max_retries: int = 0,
) -> LLMConfig:
    """Build the adapter config from settings, refusing to guess a model."""

    if not model:
        raise ChatError(
            "NYX_LLM_MODEL is not set; write it into .env -- the chat shell will not guess a model",
            kind="model_unavailable",
        )
    return LLMConfig(
        protocol=protocol,  # type: ignore[arg-type]
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )
