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
    "PLAN_PROMPT",
    "PlanRefusedError",
    "PlanScript",
    "compose_prompt",
]

PLAN_PROMPT: Final[str] = "Run the planned tool call."
"""The user prompt of a planned call's run. Read by no model (the script answers), never stored."""

COMPOSE_INSTRUCTIONS: Final[str] = (
    "The tools below were already run for this question; no other tool is available. Answer the "
    "question using ONLY these results. Do not invent, estimate or round any number, date, name "
    "or fact that is not in them. If they do not answer the question, say so plainly."
)


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
