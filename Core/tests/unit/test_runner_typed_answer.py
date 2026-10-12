"""C9 (D44): a profile with an `answer_schema` runs with structured output.

Subject: adapters/driven/agent_pydantic/runner.py (`_agent_for`, `_outcome`).

The model is a `FunctionModel`: it is handed the output tool the runner registered (or none) and
answers with a scripted list of tool-call arguments. The turn must come back as compact JSON text,
an output that does not match the schema must be sent back to the model ONCE, and a second miss
must fail the turn (the peer workflow turns that into a failed answer).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.domain.policy import RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

SCHEMA = {
    "type": "object",
    "properties": {
        "risk": {"type": "string", "enum": ["low", "high"]},
        "score": {"type": "number"},
    },
    "required": ["risk", "score"],
    "additionalProperties": False,
}


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1", channel="http", tenant_id=TenantId("t-1"), roles=frozenset({"operator"})
    )


def _request() -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id="s-1", tenant_id=TenantId("t-1")),  # type: ignore[arg-type]
        caller=_caller(),
        profile_id="scorer",
        input=UserInput(text="score this account"),
    )


def _profile(**extra: Any) -> AgentProfile:
    return AgentProfile.from_mapping(
        {"id": "scorer", "persona": "You score accounts.", "model": "m", **extra}
    )


class _Scripted:
    __name__ = "scripted"

    def __init__(self, *outputs: dict[str, Any]) -> None:
        self.outputs = list(outputs)
        self.requests = 0
        self.output_tool_names: list[list[str]] = []
        self.allow_text: list[bool] = []
        self.retry_prompts: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests += 1
        self.output_tool_names.append([tool.name for tool in info.output_tools])
        self.allow_text.append(info.allow_text_output)
        for message in messages:
            for part in message.parts:
                if isinstance(part, RetryPromptPart):
                    self.retry_prompts.append(str(part.content))
        if not info.output_tools:
            return ModelResponse(parts=[TextPart("risk is low")])
        args = self.outputs.pop(0)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])


def _runner(scripted: _Scripted) -> Any:
    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(scripted)

    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"m": "m"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(_caller(), ())),
        audit=fakes.FakeAuditSink(),
        model_factory=factory,
    )


def _run(scripted: _Scripted, profile: AgentProfile) -> Any:
    return asyncio.run(_runner(scripted).run(TurnId("turn-1"), _request(), profile, None))


def test_a_typed_profile_answers_with_its_structured_output_as_compact_json() -> None:
    scripted = _Scripted({"risk": "low", "score": 0.25})

    outcome = _run(scripted, _profile(answer_schema=SCHEMA))

    assert outcome.result is not None
    assert outcome.result.text == '{"risk":"low","score":0.25}'
    assert scripted.output_tool_names[0], "the model was handed an output tool for the schema"
    assert scripted.allow_text == [False], "prose is not an allowed way to finish a typed turn"


def test_output_that_does_not_match_is_sent_back_once_and_then_accepted() -> None:
    scripted = _Scripted({"risk": "medium", "score": 1}, {"risk": "high", "score": 0.9})

    outcome = _run(scripted, _profile(answer_schema=SCHEMA))

    assert outcome.result is not None
    assert outcome.result.text == '{"risk":"high","score":0.9}'
    assert scripted.requests == 2
    assert len(scripted.retry_prompts) >= 1 and "risk" in scripted.retry_prompts[0]


def test_a_second_miss_fails_the_turn() -> None:
    scripted = _Scripted(
        {"risk": "medium", "score": 1}, {"score": "high"}, {"risk": "low", "score": 1}
    )

    with pytest.raises(UnexpectedModelBehavior):
        _run(scripted, _profile(answer_schema=SCHEMA))

    assert scripted.requests == 2, "one retry, not a loop"


def test_a_profile_without_a_schema_keeps_answering_in_prose() -> None:
    scripted = _Scripted()

    outcome = _run(scripted, _profile())

    assert outcome.result is not None and outcome.result.text == "risk is low"
    assert scripted.output_tool_names == [[]]
    assert scripted.allow_text == [True]
