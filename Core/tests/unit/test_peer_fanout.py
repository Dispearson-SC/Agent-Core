"""Several `ask_peer` calls in one model step resolve together, and the turn resumes ONCE.

Subject: adapters/driving/workflow/turn_workflow.py (`run_turn_workflow`, the peer round),
adapters/driven/agent_pydantic/runner.py (`resume`).

ROOT CAUSE (found by `test_pydantic_ai_refuses_a_partial_resume` below, then fixed in the
workflow): Pydantic AI resumes a run that stopped on N deferred calls only when it is
handed results for ALL N. The workflow used to wake on each `PeerAnswer` and resume with
that one answer, so the first reply of a two-ask step raised `UserError: Tool call results
need to be provided for all deferred tool calls`. The vertical that hit it worked around it
in the tool (one `ask_peer` per step); the fix belongs here: collect every answer of the
round, then resume once.

The workflow BODY is driven without DBOS: the steps that touch the world are swapped for
recorders (the seam `tests/integration/test_durability.py` uses), the mailbox and the
`ResumeTurn` seat are fakes, and `DBOS.recv_async` replays a script of wake-ups.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.adapters.driven.tools.peers import build_toolset
from agent_core.adapters.driving.channels.registry import ChannelRegistry
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    UserInput,
)
from tests.fakes import ports as fakes

ASKER = "p-asker"
PEER = "p-peer"
REPLY_TIMEOUT = 600


def _request() -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t")),
        caller=CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t")),
        profile_id=ASKER,
        input=UserInput(text="ask both"),
    )


def _ask(tool_call_id: str, *, target: str = PEER, question: str = "q") -> PendingRequest:
    return PendingRequest(
        kind=PendingKind.DELEGATION,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name="ask_peer",
        arguments={"target": target, "question": f"{question}-{tool_call_id}"},
        reason="asking",
    )


def _profiles() -> dict[str, AgentProfile]:
    def side(me: str, other: str) -> AgentProfile:
        return AgentProfile(
            id=me,
            persona=me,
            model="m",
            peers=PeerPolicy(
                enabled=True,
                peers=(AgentRef(agent_id=AgentId(other), display_name=other),),
                max_hops=1,
                reply_timeout_seconds=REPLY_TIMEOUT,
            ),
        )

    return {ASKER: side(ASKER, PEER), PEER: side(PEER, ASKER)}


class _Mailbox:
    def __init__(self) -> None:
        self.asks: list[tuple[str, str]] = []
        self.answers: dict[str, str] = {}
        self._ids: dict[tuple[str, str], str] = {}

    async def ask(self, policy: Any, target: AgentId, question: str, **_: Any) -> str:
        key = (str(target), question)
        if key not in self._ids:  # idempotent per (target, question), like the real one
            self._ids[key] = f"corr-{len(self._ids) + 1}"
            self.asks.append(key)
        return self._ids[key]

    async def read_answer(self, correlation_id: str) -> str | None:
        raw = self.answers.get(correlation_id)
        return None if raw is None else wrap_peer_answer(raw)


class _Resume:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, object], ...]] = []
        self.then: list[TurnOutcome] = []  # outcomes to return before finishing

    async def execute(
        self, turn_id: TurnId, session: Any, profile_id: str, resolutions: Any, *, caller: Any
    ) -> TurnOutcome:
        self.calls.append(tuple((str(r.tool_call_id), r.payload) for r in resolutions))
        if self.then:
            return self.then.pop(0)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="done"))


@dataclass
class _World:
    mailbox: _Mailbox = field(default_factory=_Mailbox)
    resume: _Resume = field(default_factory=_Resume)
    recv_timeouts: list[float] = field(default_factory=list)
    clock: list[float] = field(default_factory=lambda: [1000.0])
    script: list[Any] = field(default_factory=list)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    pending: tuple[PendingRequest, ...],
    script: list[Callable[[_World], Any]],
) -> _World:
    """Bind the fakes. Each script item runs when the body waits: it may answer in the
    mailbox, advance the clock, and returns what `recv_async` hands back."""
    world = _World()

    async def new_turn_id() -> TurnId:
        return TurnId("turn-1")

    async def start(turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, pending=pending)

    async def noop(*_: Any, **__: Any) -> None:
        return None

    async def drain(request: TurnRequest) -> TurnRequest:
        return request

    async def clock() -> float:
        return world.clock[0]

    class _Dbos:
        @staticmethod
        async def recv_async(topic: str | None = None, timeout_seconds: float = 60) -> Any:
            world.recv_timeouts.append(timeout_seconds)
            return script.pop(0)(world)

    monkeypatch.setattr(turn_workflow, "_step_drain_pending", drain)
    monkeypatch.setattr(turn_workflow, "_step_new_turn_id", new_turn_id)
    monkeypatch.setattr(turn_workflow, "_step_start", start)
    monkeypatch.setattr(turn_workflow, "_step_publish", noop)
    monkeypatch.setattr(turn_workflow, "_step_deliver", noop)
    monkeypatch.setattr(turn_workflow, "_step_compact", noop)
    monkeypatch.setattr(turn_workflow, "_step_peer_clock", clock)
    monkeypatch.setattr(turn_workflow, "DBOS", _Dbos)
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", None),
            channels=ChannelRegistry(()),
            resume_turn=cast("Any", world.resume),
            peers=turn_workflow.PeerSeat(mailbox=cast("Any", world.mailbox), profiles=_profiles()),
        ),
    )
    return world


def _body() -> Any:
    return inspect.unwrap(turn_workflow.run_turn_workflow)


def _answer(corr: str, text: str) -> Callable[[_World], Any]:
    def run(world: _World) -> Any:
        world.mailbox.answers[corr] = text
        return turn_workflow.PeerAnswer(turn_id=TurnId("turn-1"), correlation_id=corr)

    return run


def _nobody_answers(advance: float) -> Callable[[_World], Any]:
    def run(world: _World) -> Any:
        world.clock[0] += advance
        return None

    return run


def _run(world: _World) -> TurnOutcome:
    del world
    return asyncio.run(_body()(_request()))


# --- what actually happens today -----------------------------------------------------------


def test_pydantic_ai_refuses_a_partial_resume() -> None:
    """The constraint the workflow must meet: results for ALL deferred calls, at once."""
    calls = [0]

    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls[0] += 1
        return ModelResponse(
            parts=[
                ToolCallPart("ask_peer", {"target": "a", "question": "1"}, tool_call_id="c1"),
                ToolCallPart("ask_peer", {"target": "b", "question": "2"}, tool_call_id="c2"),
            ]
            if calls[0] == 1
            else [TextPart("done")],
            usage=RequestUsage(input_tokens=1, output_tokens=1),
        )

    caller = _request().caller
    runner = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(caller, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(build_toolset(), ("ask_peer",)),
        model_factory=lambda model_id, base_url: FunctionModel(scripted),
    )
    profile = AgentProfile.from_mapping({"id": "p", "persona": "P", "model": "minimax/MiniMax-M3"})
    session = SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t"))
    first = asyncio.run(
        runner.run(
            TurnId("t1"),
            TurnRequest(session=session, caller=caller, profile_id="p", input=UserInput("hi")),
            profile,
            None,
        )
    )
    assert [str(p.tool_call_id) for p in first.pending] == ["c1", "c2"]

    class _One:
        tool_call_id = ToolCallId("c1")
        approved = True
        payload: object | None = "answer"

    with pytest.raises(UserError, match="all deferred tool calls"):
        asyncio.run(
            runner.resume(
                TurnId("t1"),
                profile,
                list(first.messages),
                (_One(),),
                caller=caller,
                session=session,
            )
        )


# --- the workflow collects every answer, then resumes once ----------------------------------


def test_two_asks_in_one_step_resume_the_turn_once_with_both_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _install(
        monkeypatch,
        (_ask("tc-2"), _ask("tc-1")),  # deliberately unsorted
        # corr-1 is tc-1's (asks go out in tool_call_id order); the slower peer answers first
        [_answer("corr-2", "slow"), _answer("corr-1", "fast")],
    )

    outcome = _run(world)

    assert outcome.result is not None
    assert len(world.resume.calls) == 1, "one resume for the whole step"
    ((first_id, first_payload), (second_id, second_payload)) = world.resume.calls[0]
    assert [first_id, second_id] == ["tc-1", "tc-2"], "results follow the tool_call ids"
    assert first_payload == wrap_peer_answer("fast")
    assert second_payload == wrap_peer_answer("slow")
    assert [target for target, _ in world.mailbox.asks] == [PEER, PEER]


def test_one_ask_timing_out_yields_an_error_for_that_call_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _install(
        monkeypatch,
        (_ask("tc-1"), _ask("tc-2")),
        [_answer("corr-1", "ok"), _nobody_answers(REPLY_TIMEOUT)],
    )

    _run(world)

    ((first_id, first_payload), (second_id, second_payload)) = world.resume.calls[0]
    assert (first_id, first_payload) == ("tc-1", wrap_peer_answer("ok"))
    assert second_id == "tc-2"
    assert isinstance(second_payload, str)
    assert "did not answer" in second_payload
    assert len(world.resume.calls) == 1


def test_the_wait_is_bounded_by_the_reply_timeout_not_by_three_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def later(advance: float, corr: str) -> Callable[[_World], Any]:
        def run(world: _World) -> Any:
            world.clock[0] += advance
            return _answer(corr, "x")(world)

        return run

    world = _install(
        monkeypatch,
        (_ask("tc-1"), _ask("tc-2")),
        [later(100, "corr-1"), later(100, "corr-2")],
    )

    _run(world)

    # the wait was opened with the full reply_timeout, then shrank by the time already spent
    assert world.recv_timeouts == [REPLY_TIMEOUT, REPLY_TIMEOUT - 100]


def test_a_duplicate_wake_for_an_answered_ask_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _install(
        monkeypatch,
        (_ask("tc-1"), _ask("tc-2")),
        [_answer("corr-1", "a"), _answer("corr-1", "a"), _answer("corr-2", "b")],
    )

    _run(world)

    assert len(world.resume.calls) == 1
    assert [tool for tool, _ in world.resume.calls[0]] == ["tc-1", "tc-2"]


def test_a_refused_ask_and_a_live_one_resume_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _install(
        monkeypatch,
        (_ask("tc-1", target="nobody-home"), _ask("tc-2")),
        [_answer("corr-1", "fine")],
    )

    _run(world)

    assert len(world.resume.calls) == 1, "the refusal waits for the live ask, no partial resume"
    ((refused_id, refused_text), (live_id, live_payload)) = world.resume.calls[0]
    assert refused_id == "tc-1"
    assert "refused before it was sent" in str(refused_text)
    assert (live_id, live_payload) == ("tc-2", wrap_peer_answer("fine"))


def test_a_late_answer_from_a_timed_out_ask_does_not_resume_the_next_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1: tc-2 times out. Round 2 (one ask) wakes first on tc-2's late reply, which
    must be skipped - resuming on it would name a tool call already answered."""
    world = _install(
        monkeypatch,
        (_ask("tc-1"), _ask("tc-2")),
        [
            _answer("corr-1", "ok"),
            _nobody_answers(REPLY_TIMEOUT),
            _answer("corr-2", "too late"),  # round 2 starts here
            _answer("corr-3", "second round"),
        ],
    )
    world.resume.then = [TurnOutcome(turn_id=TurnId("turn-1"), pending=(_ask("tc-3"),))]

    outcome = _run(world)

    assert outcome.result is not None
    assert [[tool for tool, _ in call] for call in world.resume.calls] == [
        ["tc-1", "tc-2"],
        ["tc-3"],
    ]
    assert world.resume.calls[1][0][1] == wrap_peer_answer("second round")
