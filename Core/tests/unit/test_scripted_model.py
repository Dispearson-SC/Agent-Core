"""The scripted model helper (tests/fakes/scripted_model.py) behaves as documented."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from tests.fakes.scripted_model import (
    ScriptedModel,
    ScriptedModelExhaustedError,
    fail,
    hang,
    reply,
    text,
    tool_call,
    tool_calls,
)


def _agent(model: ScriptedModel) -> Agent[None, str]:
    agent: Agent[None, str] = Agent(model.model)

    @agent.tool_plain
    def echo(value: str) -> str:
        return f"echo:{value}"

    return agent


def test_responses_are_consumed_in_order_and_requests_are_captured() -> None:
    model = ScriptedModel().push(tool_call("echo", {"value": "x"}, id="c1"), text("final"))

    result = asyncio.run(_agent(model).run("hello"))

    assert result.output == "final"
    assert model.call_count == 2 and len(model.requests) == 2
    second = model.requests[1]
    returns = [p for m in second for p in m.parts if isinstance(p, ToolReturnPart)]
    assert [(r.tool_call_id, r.content) for r in returns] == [("c1", "echo:x")]


def test_several_tool_calls_in_one_response_come_back_together() -> None:
    model = ScriptedModel().push(
        tool_calls(
            tool_call("echo", {"value": "a"}, id="c1"), tool_call("echo", {"value": "b"}, id="c2")
        ),
        text("ok"),
    )

    asyncio.run(_agent(model).run("go"))

    assert model.call_count == 2
    last = model.requests[1][-1]
    assert [p.tool_call_id for p in last.parts if isinstance(p, ToolReturnPart)] == ["c1", "c2"]


def test_reply_mixes_parts_and_missing_ids_are_generated() -> None:
    combined = reply(text("thinking"), tool_call("echo", {"value": "v"}))

    assert isinstance(combined.parts[0], TextPart)
    call = combined.parts[1]
    assert isinstance(call, ToolCallPart) and call.tool_call_id.startswith("scripted-call-")


def test_serve_is_the_fallback_after_the_queue_and_sees_the_messages() -> None:
    seen: list[int] = []
    model = (
        ScriptedModel().push(text("first")).serve(lambda m, i: (seen.append(len(m)), text("x"))[1])
    )

    async def run() -> list[str]:
        agent: Agent[None, str] = Agent(model.model)
        return [(await agent.run("1")).output, (await agent.run("2")).output]

    assert asyncio.run(run()) == ["first", "x"]
    assert seen == [1]


def test_an_exhausted_script_fails_loudly() -> None:
    model = ScriptedModel()
    with pytest.raises(ScriptedModelExhaustedError):
        asyncio.run(_agent(model).run("hi"))


def test_fail_raises_the_given_error() -> None:
    model = ScriptedModel().push(fail(RuntimeError("provider down")))
    with pytest.raises(RuntimeError, match="provider down"):
        asyncio.run(_agent(model).run("hi"))


def test_hang_never_returns_until_cancelled() -> None:
    model = ScriptedModel().push(hang())

    async def run() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_agent(model).run("hi"), 0.2)

    asyncio.run(run())
    assert model.call_count == 1


def test_a_gate_holds_the_call_until_released() -> None:
    model = ScriptedModel().push(text("late"))
    gate = model.gate()

    async def run() -> None:
        task = asyncio.create_task(_agent(model).run("hi"))
        await asyncio.wait_for(gate.started.wait(), 2)
        await asyncio.sleep(0.05)
        assert not task.done(), "the model is held mid-flight"
        gate.release()
        assert (await asyncio.wait_for(task, 2)).output == "late"

    asyncio.run(run())


def test_wait_for_calls_returns_when_the_model_was_entered_n_times() -> None:
    model = ScriptedModel().push(tool_call("echo", {"value": "x"}), text("done"))

    async def run() -> None:
        task = asyncio.create_task(_agent(model).run("hi"))
        await model.wait_for_calls(2)
        await task

    asyncio.run(run())
    assert model.call_count == 2


def test_a_ready_made_model_response_passes_through() -> None:
    model = ScriptedModel().push(ModelResponse(parts=[TextPart("raw")]))
    assert asyncio.run(_agent(model).run("hi")).output == "raw"
