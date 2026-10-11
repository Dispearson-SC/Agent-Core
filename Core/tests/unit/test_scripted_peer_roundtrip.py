"""End to end, no network: a REAL `PydanticAgentRunner` driven by a scripted model.

The orchestrator model emits TWO `ask_peer` calls in one response; a fake peer answers both
(out of order); the workflow body collects both and resumes the turn ONCE, and the model's
second call sees both tool results (resolved as one `tool_call_id`-ordered batch), each
wrapped as untrusted content.

Unlike `test_peer_fanout.py` (which stubs the runner), start and resume here go through the
production runner and Pydantic AI; only the ports around it are fakes. The DBOS workflow body
is driven the way `tests/fakes/fanout.py` does (steps that touch the world are swapped).
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic_ai.messages import ToolReturnPart

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.adapters.driven.tools.peers import build_toolset
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import TurnId, TurnOutcome, TurnRequest
from tests.fakes import fanout
from tests.fakes import ports as fakes
from tests.fakes.scripted_model import ScriptedModel, text, tool_call, tool_calls


def test_two_ask_peer_calls_from_a_scripted_model_resume_once_in_id_order(
    monkeypatch: Any,
) -> None:
    model = ScriptedModel().push(
        tool_calls(
            # emitted out of tool_call_id order on purpose
            tool_call("ask_peer", {"target": fanout.PEER, "question": "second?"}, id="c2"),
            tool_call("ask_peer", {"target": fanout.PEER, "question": "first?"}, id="c1"),
        ),
        text("both answered"),
    )
    request = fanout.request()
    runner = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(
            RuleSet.for_caller(request.caller, (), default_effect=Effect.ALLOW)
        ),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(build_toolset(), ("ask_peer",)),
        model_factory=lambda model_id, base_url: model.model,
    )
    profile = AgentProfile.from_mapping(
        {"id": fanout.ASKER, "persona": "P", "model": "minimax/MiniMax-M3"}
    )
    history: dict[str, object] = {}

    async def start(turn_id: TurnId, req: TurnRequest) -> TurnOutcome:
        outcome = await runner.run(turn_id, req, profile, None)
        history["messages"] = list(outcome.messages)
        return outcome

    handed: list[list[str]] = []

    class _Seat:
        async def execute(
            self, turn_id: TurnId, session: Any, profile_id: str, resolutions: Any, *, caller: Any
        ) -> TurnOutcome:
            handed.append([str(r.tool_call_id) for r in resolutions])
            return await runner.resume(
                turn_id,
                profile,
                history["messages"],
                tuple(resolutions),
                caller=caller,
                session=session,
            )

    # the peer asks go out in tool_call_id order: "first?" -> corr-1 (c1), "second?" -> corr-2
    world = fanout.install(
        monkeypatch,
        (),  # unused: start_step supplies the real outcome
        [fanout.answer("corr-2", "slow one"), fanout.answer("corr-1", "fast one")],
        start_step=start,
        resume_seat=_Seat(),
    )

    outcome = asyncio.run(fanout.body()(request))

    assert outcome.result is not None and outcome.result.text == "both answered"
    assert model.call_count == 2, "one run, ONE resume"
    returns = [
        part
        for message in model.requests[1]
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    # the workflow handed the runner ONE batch in tool_call_id order (Pydantic AI then
    # lays the returns out in the model's own call order, so compare by id)
    assert handed == [["c1", "c2"]]
    assert {r.tool_call_id: r.content for r in returns} == {
        "c1": wrap_peer_answer("fast one"),
        "c2": wrap_peer_answer("slow one"),
    }
    assert [q for _, q in world.mailbox.asks] == ["first?", "second?"]
