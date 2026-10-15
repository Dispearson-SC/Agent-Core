"""The runner sends a profile's `model_settings` (D99) on its turns and on their resumes.

A peer answer is a turn of the callee's profile, so it is covered by the same path. A provider that
refuses `reasoning_effort` gets the call once more without it (the compose rule, slice 39), but only
while nothing of the run has executed yet.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

CALLER = CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t"))
SESSION = SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t"))
REFUSAL = {"error": {"message": "UnsupportedParamsError: reasoning_effort is not supported"}}


@dataclass
class Resolution:
    tool_call_id: ToolCallId
    approved: bool
    payload: object | None = None


class Recorder:
    """A model body that records the settings of every request and may refuse the effort."""

    __name__ = "recorder"

    def __init__(self, *, refuse_effort: bool = False) -> None:
        self.settings: list[dict[str, Any]] = []
        self.refuse_effort = refuse_effort

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        settings = dict(info.model_settings or {})
        self.settings.append(settings)
        if self.refuse_effort and "openai_reasoning_effort" in settings:
            raise ModelHTTPError(400, "recorder", REFUSAL)
        return ModelResponse(parts=[TextPart("done")], usage=RequestUsage(output_tokens=1))


def _profile(**extra: object) -> AgentProfile:
    return AgentProfile.from_mapping(
        {"id": "p", "persona": "P", "model": "minimax/MiniMax-M3", **extra}
    )


def _runner(model: Recorder) -> Any:
    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(CALLER, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        model_factory=lambda model_id, base_url: FunctionModel(model),
    )


def _request() -> TurnRequest:
    return TurnRequest(session=SESSION, caller=CALLER, profile_id="p", input=UserInput(text="hi"))


def _run(runner: Any, profile: AgentProfile) -> Any:
    return asyncio.run(runner.run(TurnId("turn-1"), _request(), profile, None))


def _suspended() -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart("ask the other agent")]),
        ModelResponse(
            parts=[ToolCallPart("ask_other", {"q": "x"}, tool_call_id="call-1")],
            usage=RequestUsage(output_tokens=3),
        ),
    ]


def _resume(runner: Any, profile: AgentProfile, resolution: Resolution) -> Any:
    return asyncio.run(
        runner.resume(
            TurnId("turn-1"),
            profile,
            _suspended(),
            (resolution,),
            caller=CALLER,
            session=SESSION,
        )
    )


TUNED = {"model_settings": {"reasoning_effort": "low", "max_tokens": 2048}}


def test_a_turn_sends_the_profiles_reasoning_effort_and_output_bound() -> None:
    model = Recorder()

    outcome = _run(_runner(model), _profile(**TUNED))

    assert outcome.result is not None and outcome.result.text == "done"
    assert model.settings[0]["openai_reasoning_effort"] == "low"
    assert model.settings[0]["max_tokens"] == 2048


def test_a_profile_without_the_block_sends_neither_as_before() -> None:
    model = Recorder()

    _run(_runner(model), _profile())

    assert "openai_reasoning_effort" not in model.settings[0]
    assert "max_tokens" not in model.settings[0]


def test_a_resumed_turn_sends_the_same_settings() -> None:
    model = Recorder()

    outcome = _resume(
        _runner(model), _profile(**TUNED), Resolution(ToolCallId("call-1"), True, "the answer")
    )

    assert outcome.result is not None and outcome.result.text == "done"
    assert model.settings[0]["openai_reasoning_effort"] == "low"
    assert model.settings[0]["max_tokens"] == 2048


def test_a_refused_effort_is_dropped_and_the_turn_asked_once_more() -> None:
    model = Recorder(refuse_effort=True)

    outcome = _run(_runner(model), _profile(**TUNED))

    assert outcome.result is not None and outcome.result.text == "done"
    assert len(model.settings) == 2
    assert "openai_reasoning_effort" not in model.settings[1]
    assert model.settings[1]["max_tokens"] == 2048, "only the refused parameter is dropped"


def test_a_refused_effort_on_a_resume_with_answers_only_is_asked_once_more() -> None:
    model = Recorder(refuse_effort=True)

    outcome = _resume(
        _runner(model), _profile(**TUNED), Resolution(ToolCallId("call-1"), True, "the answer")
    )

    assert outcome.result is not None and outcome.result.text == "done"
    assert [("openai_reasoning_effort" in s) for s in model.settings] == [True, False]


def test_a_refused_effort_is_not_retried_after_a_bare_approval_ran_its_tool() -> None:
    """A bare approval runs the deferred tool before the first request: asking again would run it
    twice. The refusal is the turn's error, as any other provider error is."""
    model = Recorder(refuse_effort=True)

    with pytest.raises(ModelHTTPError):
        _resume(_runner(model), _profile(**TUNED), Resolution(ToolCallId("call-1"), True))

    assert len(model.settings) == 1


def test_any_other_provider_error_is_not_retried() -> None:
    class Broken(Recorder):
        def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.settings.append(dict(info.model_settings or {}))
            raise ModelHTTPError(400, "recorder", {"error": {"message": "context too long"}})

    model = Broken()

    with pytest.raises(ModelHTTPError):
        _run(_runner(model), _profile(**TUNED))

    assert len(model.settings) == 1
