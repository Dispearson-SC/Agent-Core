"""Driven adapter helpers: one planned tool call, and the compose prompt, over Pydantic AI (D97).

Phase:   Phase 5 (fast communication), P3
Tests:   Core/tests/unit/test_intent_runner.py
Used by: runner.py (`PydanticAgentRunner.call_tool`, `.compose_reply`, `.template_reply`)

WHY A SCRIPTED MODEL RUNS THE CALL
    A planned call must go through EXACTLY the path a model's call goes through: the profile's
    toolset (its grant and its wrappers), `PolicyEnforcement` (the verdict, the audit row and its
    `tool_called` event, the approval deferral), `UntrustedResultWrapping` (the result budget and
    the delimiters) and the profile's usage limits. Re-implementing that pipeline beside the runner
    would be a second enforcement point that drifts. So the call is one `agent.run` of the profile's
    own cached agent whose MODEL is `PlanScript`: a `FunctionModel` function that emits the planned
    `ToolCallPart` on its first request and ends the run on its second, reading back how the call
    ended. No provider is called and nothing is billed.

    Before it emits the call, the script checks the tool against what the run offers (the tool
    must be in the profile's grant: `tool_not_granted`) and against its definition (a plain
    function tool declared `replay: safe`, else `tool_not_read_only`). Registration already refused
    write tools by name; this is the same rule at the moment of the call, from the definition the
    model would have seen.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo

from agent_core.domain.intents import (
    PLAN_ARGUMENT_INVALID,
    PLAN_POLICY_DENIED,
    PLAN_TOOL_ERROR,
    PLAN_TOOL_NOT_GRANTED,
    PLAN_TOOL_NOT_READ_ONLY,
    PlannedCall,
    PlanToolResult,
)
from agent_core.domain.tool_replay import ReplayPolicy, replay_policy

__all__ = [
    "COMPOSE_INSTRUCTIONS",
    "COMPOSITE_INSTRUCTIONS",
    "DEFAULT_COMPOSE_MAX_TOKENS",
    "DEFAULT_COMPOSE_REASONING_EFFORT",
    "REASONING_EFFORTS",
    "ComposeIncompleteError",
    "ComposeSettings",
    "PLAN_PROMPT",
    "PlanRefusedError",
    "PlanScript",
    "compose_prompt",
    "composite_prompt",
    "refuses_reasoning_effort",
]

PLAN_PROMPT: Final[str] = "Run the planned tool call."
"""The user prompt of a planned call's run. Read by no model (the script answers), never stored."""

COMPOSE_INSTRUCTIONS: Final[str] = (
    "The tools below were already run for this question; no other tool is available. Answer the "
    "question directly and briefly, from these results only: lead with what matters, use at most "
    "a few short bullets, and leave out what the question did not ask for. Copy numbers, dates "
    "and names exactly as they appear in the results; never compute new ones. If the results do "
    "not answer the question, say so plainly."
)
"""Measured on the live provider (L18, a reasoning model): the earlier wording, which listed every
kind of fact not to invent, estimate or round, made it verify each figure and spend 6-7k reasoning
tokens (33-38 s) composing from an 11k-token result; this wording about 1k (7-8 s)."""

COMPOSITE_INSTRUCTIONS: Final[str] = (
    "The results are grouped by topic. The question may ask about several of them: answer every "
    "topic it asks about, in the order it asks, each in a sentence or two."
)
"""S10. Added to `COMPOSE_INSTRUCTIONS` for a fast path composite (several intents, one reply)."""

DEFAULT_COMPOSE_MAX_TOKENS: Final = 4096
DEFAULT_COMPOSE_REASONING_EFFORT: Final = "low"
REASONING_EFFORTS: Final = frozenset({"minimal", "low", "medium", "high"})


@dataclass(frozen=True, slots=True)
class ComposeSettings:
    """The bounds of a compose call, set per deployment (`AGENT_CORE_COMPOSE_*`).

    `max_tokens` bounds the whole answer, reasoning included on a provider that counts it there;
    a reply cut at it is not a reply (`ComposeIncompleteError`) and the plan falls back.
    `reasoning_effort` is sent as the OpenAI-compatible `reasoning_effort`; None sends nothing. A
    provider or proxy that refuses the parameter (`refuses_reasoning_effort`) gets the compose once
    more without it, with a log line, instead of every plan falling back.
    Measured (L18): "low" took the same compose from 33-38 s to 3-7 s."""

    max_tokens: int = DEFAULT_COMPOSE_MAX_TOKENS
    reasoning_effort: str | None = DEFAULT_COMPOSE_REASONING_EFFORT

    def __post_init__(self) -> None:
        if self.max_tokens < 1:
            raise ValueError(f"the compose max_tokens must be positive, got {self.max_tokens}")
        if self.reasoning_effort is not None and self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                f"the compose reasoning effort must be one of {sorted(REASONING_EFFORTS)}, "
                f"got {self.reasoning_effort!r}"
            )

    def model_settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = {"max_tokens": self.max_tokens}
        if self.reasoning_effort is not None:
            settings["openai_reasoning_effort"] = self.reasoning_effort
        return settings


def refuses_reasoning_effort(status_code: int, body: object) -> bool:
    """Whether a provider error is a refusal of the `reasoning_effort` parameter (slice 39).

    Some providers and proxies answer 400 to a parameter they do not take (litellm's
    `UnsupportedParamsError` for minimax, OpenAI's "Unsupported parameter", an
    `unsupported_parameter` code). Only a 400 that names the parameter, or names an unsupported
    parameter about reasoning, counts: any other error is the compose's own failure."""
    if status_code != 400:
        return False
    text = str(body).lower()
    if "reasoning_effort" in text or "reasoning effort" in text:
        return True
    return "reasoning" in text and any(
        word in text for word in ("unsupported", "not supported", "unrecognized", "unknown")
    )


class ComposeIncompleteError(Exception):
    """A compose that came back cut at its token bound or with no text: not a reply."""


class PlanRefusedError(Exception):
    """The script refused to emit the call; `reason` is the plan fallback code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PlanScript:
    """The `FunctionModel` function of one planned call. Records the part that answered it."""

    __name__ = "planned_call"

    def __init__(self, call: PlannedCall, call_id: str, refusal_prefix: str) -> None:
        self._call = call
        self._call_id = call_id
        self._refusal_prefix = refusal_prefix
        self.answer: ToolReturnPart | RetryPromptPart | None = None

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        answered = [
            part
            for part in messages[-1].parts
            if isinstance(part, (ToolReturnPart, RetryPromptPart))
            and part.tool_call_id == self._call_id
        ]
        if answered:
            self.answer = answered[0]
            return ModelResponse(parts=[TextPart("done")])
        tool = next((t for t in info.function_tools if t.name == self._call.tool), None)
        if tool is None:
            raise PlanRefusedError(PLAN_TOOL_NOT_GRANTED)
        if tool.kind != "function" or replay_policy(tool.metadata) is not ReplayPolicy.SAFE:
            raise PlanRefusedError(PLAN_TOOL_NOT_READ_ONLY)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    self._call.tool, dict(self._call.arguments), tool_call_id=self._call_id
                )
            ]
        )

    def result(self) -> PlanToolResult:
        """How the call ended, read from the part that answered it."""
        call = self._call
        part = self.answer
        if part is None:
            return PlanToolResult(call.step_id, call.tool, failure=PLAN_TOOL_ERROR)
        if isinstance(part, RetryPromptPart):
            # A list is the library's argument validation against the tool's schema; text is a
            # `ModelRetry` the tool raised. Either way the tool did not answer.
            failure = PLAN_ARGUMENT_INVALID if isinstance(part.content, list) else PLAN_TOOL_ERROR
            return PlanToolResult(call.step_id, call.tool, failure=failure)
        content = part.content
        if isinstance(content, str) and content.startswith(self._refusal_prefix):
            return PlanToolResult(call.step_id, call.tool, failure=PLAN_POLICY_DENIED)
        if isinstance(content, Mapping) and "error" in content:
            # The tool bridge's error-as-value shape (`{"error": ..., "status"?: ...}`).
            return PlanToolResult(call.step_id, call.tool, failure=PLAN_TOOL_ERROR)
        return PlanToolResult(call.step_id, call.tool, content=_as_text(content))


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str, sort_keys=True)


def compose_prompt(question: str, results: tuple[PlanToolResult, ...]) -> str:
    """The one prompt of a compose call: the person's question, then each result by tool."""
    blocks = "\n\n".join(f"### {r.tool} ({r.step_id})\n{r.content}" for r in results)
    return f"{question}\n\n## Tool results\n\n{blocks}"


def composite_prompt(
    question: str, groups: tuple[tuple[str, tuple[PlanToolResult, ...]], ...]
) -> str:
    """S10. The one prompt of a composite compose: the question, then each intent's results under
    the intent's name, in intent order."""
    sections = []
    for intent, results in groups:
        blocks = "\n\n".join(f"### {r.tool} ({r.step_id})\n{r.content}" for r in results)
        sections.append(f"## {intent}\n\n{blocks}")
    body = "\n\n".join(sections)
    return f"{question}\n\n# Tool results by topic\n\n{body}"
