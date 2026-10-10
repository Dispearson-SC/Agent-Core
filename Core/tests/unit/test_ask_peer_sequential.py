"""Two ask_peer calls in one model step: only the first is deferred.

The workflow resumes the asking turn with one peer answer at a time, while pydantic-ai wants
results for every deferred call at once, so a second deferred call would hang the turn. The
guard lives in the tool: later ask_peer calls of the same response get a retry prompt.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core.adapters.driven.tools.peers import build_toolset


def _agent(calls: list[dict[str, str]]) -> Agent[None, Any]:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("ask_peer", c) for c in calls])

    return Agent(
        FunctionModel(model),
        toolsets=[build_toolset()],
        output_type=[str, DeferredToolRequests],
    )


def test_a_second_ask_peer_in_the_same_step_is_not_deferred() -> None:
    result = _agent(
        [
            {"target": "glazed_present", "question": "q1"},
            {"target": "glazed_supply", "question": "q2"},
        ]
    ).run_sync("hi")

    assert isinstance(result.output, DeferredToolRequests)
    assert [c.args_as_dict()["target"] for c in result.output.calls] == ["glazed_present"]
    retries = [
        p
        for m in result.all_messages()
        for p in getattr(m, "parts", [])
        if isinstance(p, RetryPromptPart)
    ]
    assert len(retries) == 1 and "one specialist" in str(retries[0].content)


def test_a_single_ask_peer_is_deferred_as_before() -> None:
    result = _agent([{"target": "glazed_present", "question": "q1"}]).run_sync("hi")

    assert isinstance(result.output, DeferredToolRequests)
    assert len(result.output.calls) == 1
