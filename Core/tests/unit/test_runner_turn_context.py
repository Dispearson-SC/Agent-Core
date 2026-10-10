"""The runner hands every tool the turn's identity as `ctx.deps` (`TurnContext`).

Subject: adapters/driven/agent_pydantic/runner.py, adapters/driven/tools/context.py

WHY THIS EXISTS
    A tool is a plain function and, until now, knew nothing about the turn it ran in. The
    Glazed vertical must bind `store_id` (the caller's tenant) and the case (the session)
    to every backend call WITHOUT the model choosing either, so the one place that knows
    them - the runner - passes them as Pydantic AI `deps`. Tools read `ctx.deps`; the model
    never sees these fields in a tool schema.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

TOOL = "whoami"
SESSION = SessionRef(session_id="case-77", tenant_id=TenantId("S030"))  # type: ignore[arg-type]
CALLER = CallerIdentity(
    subject_id="manager-1",
    channel="glazed",
    tenant_id=TenantId("S030"),
    roles=frozenset({"glazed-manager"}),
)


class _Resolution:
    def __init__(self, tool_call_id: str) -> None:
        self.tool_call_id = ToolCallId(tool_call_id)
        self.approved = True
        self.payload: object | None = None


def _model(call_first: bool) -> Any:
    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returned = any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
        if call_first and not returned:
            return ModelResponse(
                parts=[ToolCallPart(TOOL, {})],
                usage=RequestUsage(input_tokens=1, output_tokens=1),
            )
        return ModelResponse(
            parts=[TextPart("ok")], usage=RequestUsage(input_tokens=1, output_tokens=1)
        )

    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(scripted)

    return factory


def _runner(seen: list[Any], call_first: bool = True) -> Any:
    def whoami(ctx: RunContext[Any]) -> str:
        """Report the turn context."""
        seen.append(ctx.deps)
        return "seen"

    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(CALLER, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(FunctionToolset([whoami]), (TOOL,)),
        model_factory=_model(call_first),
    )


def _profile() -> AgentProfile:
    return AgentProfile.from_mapping({"id": "p", "persona": "x", "model": "minimax/MiniMax-M3"})


def test_run_hands_tools_the_caller_and_session_as_deps() -> None:
    seen: list[Any] = []
    request = TurnRequest(
        session=SESSION, caller=CALLER, profile_id="p", input=UserInput(text="hi")
    )

    asyncio.run(_runner(seen).run(TurnId("t1"), request, _profile(), None))

    assert seen == [TurnContext(caller=CALLER, session=SESSION, agent_id="p")]


def test_resume_hands_tools_the_same_context() -> None:
    seen: list[Any] = []
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("hi")]),
        ModelResponse(
            parts=[ToolCallPart(TOOL, {}, tool_call_id="call_1")],
            usage=RequestUsage(input_tokens=1, output_tokens=1),
        ),
    ]

    asyncio.run(
        _runner(seen, call_first=False).resume(
            TurnId("t1"),
            _profile(),
            history,
            (_Resolution("call_1"),),
            caller=CALLER,
            session=SESSION,
        )
    )

    assert seen == [TurnContext(caller=CALLER, session=SESSION, agent_id="p")]
