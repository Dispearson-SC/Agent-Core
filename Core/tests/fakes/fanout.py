"""Public helpers for driving the turn workflow BODY without DBOS (peer fan-out tests).

The steps that touch the world are swapped for recorders, the mailbox and the `ResumeTurn` seat
are fakes, and `DBOS.recv_async` replays a script of wake-ups. Shared by
`tests/unit/test_peer_fanout.py` and `tests/unit/test_scripted_peer_roundtrip.py`; a test module
must never import another test module's private helpers.

    world = install(monkeypatch, (ask("tc-1"),), [answer("corr-1", "hi")])
    outcome = asyncio.run(body()(request()))
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.adapters.driving.channels.registry import ChannelRegistry
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import AgentId, AgentRef, PeerAnswerRefusedError, PeerPolicy
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

ASKER = "p-asker"
PEER = "p-peer"
REPLY_TIMEOUT = 600


def request() -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t")),
        caller=CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t")),
        profile_id=ASKER,
        input=UserInput(text="ask both"),
    )


def ask(tool_call_id: str, *, target: str = PEER, question: str = "q") -> PendingRequest:
    return PendingRequest(
        kind=PendingKind.DELEGATION,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name="ask_peer",
        arguments={"target": target, "question": f"{question}-{tool_call_id}"},
        reason="asking",
    )


def profiles() -> dict[str, AgentProfile]:
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


class Mailbox:
    def __init__(self) -> None:
        self.asks: list[tuple[str, str]] = []
        self.request_keys: list[str | None] = []
        self.answer_calls: list[tuple[str, str]] = []
        self.answer_tokens: list[str | None] = []
        self.refuse_answers = False
        self.cancelled: dict[str, str] = {}  # correlation id -> reason, in cancel order
        self.turn_of: dict[str, str] = {}
        self.redelivered: dict[str, int] = {}  # correlation id -> redeliveries (the generation)
        self.answers: dict[str, str] = {}
        self._ids: dict[tuple[str, str], str] = {}

    async def ask(self, policy: Any, target: AgentId, question: str, **kwargs: Any) -> str:
        self.request_keys.append(kwargs.get("request_key"))
        key = (str(target), question)
        if key not in self._ids:  # idempotent per (target, question), like the real one
            self._ids[key] = f"corr-{len(self._ids) + 1}"
            self.asks.append(key)
            self.turn_of[self._ids[key]] = str(kwargs.get("turn_id"))
        return self._ids[key]

    async def cancel(self, correlation_id: str, reason: str) -> bool:
        """Open -> cancelled; False when answered or already cancelled (like the real row)."""
        if correlation_id in self.answers or correlation_id in self.cancelled:
            return False
        self.cancelled[correlation_id] = reason
        return True

    async def is_cancelled(self, correlation_id: str) -> bool:
        return correlation_id in self.cancelled

    async def redeliveries(self, correlation_id: str) -> int:
        return self.redelivered.get(correlation_id, 0)

    async def cancel_open(self, turn_id: str, reason: str) -> tuple[str, ...]:
        ids = sorted(
            corr
            for corr, turn in self.turn_of.items()
            if turn == str(turn_id) and corr not in self.answers and corr not in self.cancelled
        )
        for corr in ids:
            self.cancelled[corr] = reason
        return tuple(ids)

    async def answer(
        self, correlation_id: str, answer: str, *, answer_token: str | None = None
    ) -> None:
        """The answering half, first answer wins like the real row. Logged in call order.
        `refuse_answers` makes it refuse like the real row does for a forged or cancelled one."""
        self.answer_calls.append((correlation_id, answer))
        self.answer_tokens.append(answer_token)
        if self.refuse_answers or correlation_id in self.cancelled:
            raise PeerAnswerRefusedError(correlation_id)
        self.answers.setdefault(correlation_id, answer)

    async def read_answer(self, correlation_id: str) -> str | None:
        raw = self.answers.get(correlation_id)
        return None if raw is None else wrap_peer_answer(raw)


class Resume:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, object], ...]] = []
        self.approvals: list[tuple[tuple[str, bool], ...]] = []
        self.then: list[TurnOutcome] = []  # outcomes to return before finishing

    async def execute(
        self, turn_id: TurnId, session: Any, profile_id: str, resolutions: Any, *, caller: Any
    ) -> TurnOutcome:
        self.calls.append(tuple((str(r.tool_call_id), r.payload) for r in resolutions))
        self.approvals.append(tuple((str(r.tool_call_id), r.approved) for r in resolutions))
        if self.then:
            return self.then.pop(0)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="done"))


@dataclass
class World:
    mailbox: Mailbox = field(default_factory=Mailbox)
    resume: Resume = field(default_factory=Resume)
    recv_timeouts: list[float] = field(default_factory=list)
    cancelled_workflows: list[str] = field(default_factory=list)
    clock: list[float] = field(default_factory=lambda: [1000.0])
    script: list[Any] = field(default_factory=list)
    ledger: Any | None = None


def install(
    monkeypatch: pytest.MonkeyPatch,
    pending: tuple[PendingRequest, ...],
    script: list[Callable[[World], Any]],
    *,
    start_step: Callable[[TurnId, TurnRequest], Any] | None = None,
    resume_seat: Any | None = None,
    ledger: Any | None = None,
    max_attempts: int = 10,
) -> World:
    """Bind the fakes. Each script item runs when the body waits: it may answer in the
    mailbox, advance the clock, and returns what `recv_async` hands back."""
    world = World(ledger=ledger)

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

    async def expire(turn_id: TurnId) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="expired"))

    class _Dbos:
        workflow_id = "turn-1"  # the running workflow's durable id, which is also the turn id

        @staticmethod
        async def cancel_workflow_async(workflow_id: str, **_: Any) -> None:
            world.cancelled_workflows.append(workflow_id)

        @staticmethod
        async def get_workflow_status_async(workflow_id: str) -> None:
            return None  # no later generation of any peer workflow exists in these runs

        @staticmethod
        async def recv_async(topic: str | None = None, timeout_seconds: float = 60) -> Any:
            world.recv_timeouts.append(timeout_seconds)
            return script.pop(0)(world)

    monkeypatch.setattr(turn_workflow, "_step_drain_pending", drain)
    monkeypatch.setattr(turn_workflow, "_step_new_turn_id", new_turn_id)
    monkeypatch.setattr(turn_workflow, "_step_start", start_step or start)
    monkeypatch.setattr(turn_workflow, "_step_publish", noop)
    monkeypatch.setattr(turn_workflow, "_step_deliver", noop)
    monkeypatch.setattr(turn_workflow, "_step_compact", noop)
    monkeypatch.setattr(turn_workflow, "_step_expire", expire)
    monkeypatch.setattr(turn_workflow, "_step_round_clock", clock)
    monkeypatch.setattr(turn_workflow, "DBOS", _Dbos)
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", None),
            channels=ChannelRegistry(()),
            resume_turn=cast("Any", resume_seat or world.resume),
            peers=turn_workflow.PeerSeat(mailbox=cast("Any", world.mailbox), profiles=profiles()),
            attempts=ledger,
            max_attempts=max_attempts,
        ),
    )
    return world


def body() -> Any:
    return inspect.unwrap(turn_workflow.run_turn_workflow)


def answer(corr: str, text: str) -> Callable[[World], Any]:
    def run(world: World) -> Any:
        world.mailbox.answers[corr] = text
        return turn_workflow.PeerAnswer(turn_id=TurnId("turn-1"), correlation_id=corr)

    return run


def nobody_answers(advance: float) -> Callable[[World], Any]:
    def run(world: World) -> Any:
        world.clock[0] += advance
        return None

    return run
