"""C1r R2-003/R3: a subagent turn resumed after an approval keeps its restricted toolset.

Subjects: adapters/driven/agent_pydantic/runner.py (`resume`), application/resume_turn.py,
ports/agent_runner.py (`resume(..., owner=)`), adapters/driving/workflow/turn_workflow.py
(`_step_resume_round` hands the request's owner on).

`run` withholds `spawn_subagent` from a turn at its depth limit. `resume` built its context
without the owner, so a subagent that suspended for a human and was approved came back with the
tool its first half did not have. The gate would still refuse the spawn, but the model would be
offered a tool it cannot use. The same owner that restricted the first half restricts the second.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo

from agent_core.domain.agent_mode import AgentMode, DelegationKind
from agent_core.domain.turn import PeerOwner, TurnId
from tests.unit.test_spawn_subagent_tool import (
    BOSS,
    RESEARCHER,
    WRITER,
    _profile,
    _request,
    _runner,
    _world,
)


class _Approved:
    tool_call_id = "call_1"
    approved = True
    payload = None


def _offered_on_resume(owner: PeerOwner | None) -> bool:
    world = _world()
    world[RESEARCHER] = _profile(RESEARCHER, spawns=(WRITER,), mode=AgentMode.SUBAGENT)
    seen: list[AgentInfo] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart("ok")])

    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("task")]),
        ModelResponse(parts=[ToolCallPart("anything", {}, tool_call_id="call_1")]),
    ]
    request = _request(RESEARCHER)
    asyncio.run(
        _runner(world, model).resume(
            TurnId("t1"),
            world[RESEARCHER],
            history,
            (_Approved(),),
            caller=request.caller,
            session=request.session,
            owner=owner,
        )
    )
    return "spawn_subagent" in {t.name for t in seen[0].function_tools}


def _subagent_at(depth: int) -> PeerOwner:
    return PeerOwner(
        "root", "root", BOSS, "corr", kind=DelegationKind.SUBAGENT, subagent_depth=depth
    )


def test_a_resumed_subagent_at_its_depth_limit_is_not_offered_the_spawn_tool() -> None:
    assert not _offered_on_resume(_subagent_at(1))


def test_a_resumed_top_level_turn_keeps_the_spawn_tool() -> None:
    assert _offered_on_resume(None)


def test_the_workflow_hands_the_requests_owner_to_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-004 / R3-source-grep-test: drive the step and read what `ResumeTurn` was handed, instead
    of grepping the step's source for the keyword argument."""
    from agent_core.adapters.driving.workflow import turn_workflow

    handed: dict[str, Any] = {}

    class _RecordingResume:
        async def execute(self, *args: Any, **kwargs: Any) -> str:
            handed.update(kwargs)
            return "outcome"

    monkeypatch.setattr(turn_workflow, "_require_resume_turn", lambda: _RecordingResume())
    owner = _subagent_at(1)
    request = _request(RESEARCHER)
    request = replace(request, owner=owner)
    answer = turn_workflow.HumanAnswer(
        turn_id=TurnId("t1"), tool_call_id="call_1", approved=True, note=None
    )

    outcome = asyncio.run(
        turn_workflow._step_resume_round(  # noqa: SLF001
            TurnId("t1"), request, ("call_1",), (), (answer,)
        )
    )

    assert outcome == "outcome"
    assert handed["owner"] == owner
