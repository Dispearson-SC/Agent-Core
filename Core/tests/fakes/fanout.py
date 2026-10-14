"""Public helpers for driving the turn workflow BODY without DBOS (peer fan-out tests).

The steps that touch the world are swapped for recorders, the mailbox and the `ResumeTurn` seat
are fakes, and `DBOS.recv_async` replays a script of wake-ups. Shared by
`tests/unit/test_peer_fanout.py` and `tests/unit/test_scripted_peer_roundtrip.py`; a test module
must never import another test module's private helpers.

    world = install(monkeypatch, (ask("tc-1"),), [answer("corr-1", "hi")])
    outcome = asyncio.run(body()(request()))
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.adapters.driving.channels.registry import ChannelRegistry
from agent_core.adapters.driving.peers.worker import new_peer_session
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.agent_mode import DelegationKind
from agent_core.domain.peer_result import PeerResult, PeerStatus
from agent_core.domain.peers import (
    BACKGROUND_LIST_LIMIT,
    AgentId,
    AgentRef,
    BackgroundAsk,
    BackgroundKeys,
    BackgroundState,
    PeerAnswerRefusedError,
    PeerBackgroundRefusedError,
    PeerContinuationRefusedError,
    PeerPolicy,
    peer_session_key,
)
from agent_core.domain.profile import AgentProfile
from agent_core.domain.subagents import SubagentPolicy
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


def spawn(tool_call_id: str, *, agent: str = PEER, task: str = "t", **extra: Any) -> PendingRequest:
    """A deferred `spawn_subagent` call (C28). `extra`: `background`, `continue_session`."""
    return PendingRequest(
        kind=PendingKind.DELEGATION,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name="spawn_subagent",
        arguments={"agent": agent, "task": f"{task}-{tool_call_id}", **extra},
        reason="spawning",
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
        self.root_turns: list[str | None] = []  # `root_turn_id` of each ask (C16)
        # C28. The kind and subagent depth of each ask, in ask order.
        self.kinds: list[DelegationKind] = []
        self.hops: list[int] = []  # the `hop` each ask travelled with
        self.depths: list[int] = []
        self.continued: list[str | None] = []  # `continue_session` of each ask, in ask order
        self.refuse_continuation = False  # the mailbox refuses every continuation (C12)
        # C19. The return address of each ask in ask order (None = ordinary), how many background
        # asks earlier turns of the conversation still have running, and a mailbox that cannot run
        # an ask in the background.
        self.background_callers: list[Any] = []
        self.background_sessions: dict[str, Any] = {}  # correlation id -> asking session
        self.open_background = 0
        self.refuse_background = False
        self.cancelled: dict[str, str] = {}  # correlation id -> reason, in cancel order
        self.turn_of: dict[str, str] = {}
        self.key_of: dict[str, str | None] = {}  # correlation id -> request key
        self.followup_turns: set[str] = set()  # turn ids that are background follow-ups (C1o)
        self.redelivered: dict[str, int] = {}  # correlation id -> redeliveries (the generation)
        self.answers: dict[str, str] = {}
        self.failed: set[str] = set()  # correlation ids closed as a callee failure (C1l)
        # C13. The real mailboxes name an ask by its request key, so a repeat in a later round is
        # a fresh ask. `fold_repeats` keeps the old per-(target, question) folding for the tests of
        # the workflow's defence against an adapter that returns a handle it already gave out.
        self.fold_repeats = False
        self._ids: dict[tuple[str, ...], str] = {}

    async def ask(self, policy: Any, target: AgentId, question: str, **kwargs: Any) -> str:
        self.request_keys.append(kwargs.get("request_key"))
        if kwargs.get("continue_session") is not None and self.refuse_continuation:
            raise PeerContinuationRefusedError("not yours")
        if kwargs.get("background_caller") is not None and self.refuse_background:
            raise PeerBackgroundRefusedError("not here")
        self.kinds.append(kwargs.get("kind", DelegationKind.PEER))
        self.hops.append(kwargs.get("hop", -1))
        self.depths.append(kwargs.get("subagent_depth", 0))
        self.continued.append(kwargs.get("continue_session"))
        self.background_callers.append(kwargs.get("background_caller"))
        self.root_turns.append(kwargs.get("root_turn_id"))
        key: tuple[str, ...] = (str(target), question)
        if not self.fold_repeats:
            key += (str(kwargs.get("request_key")),)
        if key not in self._ids:  # idempotent per request key, like the real one
            self._ids[key] = f"corr-{len(self._ids) + 1}"
            self.asks.append((str(target), question))
            if kwargs.get("background_caller") is not None:
                self.background_sessions[self._ids[key]] = kwargs.get("from_session")
            self.turn_of[self._ids[key]] = str(kwargs.get("turn_id"))
            self.key_of[self._ids[key]] = kwargs.get("request_key")
        return self._ids[key]

    async def cancel(self, correlation_id: str, reason: str) -> bool:
        """Open -> cancelled; False when answered or already cancelled (like the real row)."""
        if correlation_id in self.answers or correlation_id in self.cancelled:
            return False
        self.cancelled[correlation_id] = reason
        return True

    async def is_cancelled(self, correlation_id: str) -> bool:
        return correlation_id in self.cancelled

    async def count_background(
        self,
        session: Any,
        *,
        open_only: bool = False,
        excluding: BackgroundKeys | None = None,
    ) -> int:
        asks = await self.background_asks(session, limit=10**6)
        return sum(
            1
            for ask in asks
            if (ask.is_open or not open_only)
            and not (
                excluding is not None
                and self.turn_of.get(ask.handle) == excluding.turn_id
                and self.key_of.get(ask.handle) in excluding.keys
            )
        )

    async def background_ask(self, session: Any, handle: str) -> BackgroundAsk | None:
        for ask in await self.background_asks(session, limit=10**6):
            if ask.handle == handle:
                return ask
        return None

    async def is_background_followup(self, session: Any, turn_id: str) -> bool:
        return turn_id in self.followup_turns

    async def background_asks(
        self, session: Any, *, limit: int = BACKGROUND_LIST_LIMIT
    ) -> tuple[BackgroundAsk, ...]:
        """The conversation's background asks: `open_background` synthetic running ones from
        earlier turns, plus the ones this mailbox took (C19)."""
        at = datetime(2026, 1, 1, tzinfo=UTC)
        earlier = tuple(
            BackgroundAsk(
                handle=f"open-{n}", target=PEER, state=BackgroundState.RUNNING, question="q",
                asked_at=at,
            )
            for n in range(self.open_background)
        )
        mine = tuple(
            BackgroundAsk(
                handle=corr,
                target=PEER,
                state=(
                    BackgroundState.CANCELLED if corr in self.cancelled
                    else BackgroundState.ANSWERED if corr in self.answers
                    else BackgroundState.RUNNING
                ),
                question="q",
                asked_at=at,
            )
            for corr, owner in self.background_sessions.items()
            if owner == session
        )
        return (earlier + mine)[:limit]

    async def cancel_background(self, session: Any, reason: str) -> tuple[str, ...]:
        ids = sorted(
            corr
            for corr, owner in self.background_sessions.items()
            if owner == session and corr not in self.answers and corr not in self.cancelled
        )
        for corr in ids:
            self.cancelled[corr] = reason
        return tuple(ids)

    async def redeliveries(self, correlation_id: str) -> int:
        return self.redelivered.get(correlation_id, 0)

    async def cancel_open(self, turn_id: str, reason: str) -> tuple[str, ...]:
        ids = sorted(
            corr
            for corr, turn in self.turn_of.items()
            if turn == str(turn_id)
            and corr not in self.answers
            and corr not in self.cancelled
            and corr not in self.background_sessions  # a background ask outlives its turn (C19)
        )
        for corr in ids:
            self.cancelled[corr] = reason
        return tuple(ids)

    async def answer(
        self,
        correlation_id: str,
        answer: str,
        *,
        answer_token: str | None = None,
        failed: bool = False,
    ) -> None:
        """The answering half, first answer wins like the real row. Logged in call order.
        `refuse_answers` makes it refuse like the real row does for a forged or cancelled one.
        `failed` is the outcome as DATA (C1l): the row records it beside the text."""
        self.answer_calls.append((correlation_id, answer))
        self.answer_tokens.append(answer_token)
        if self.refuse_answers or correlation_id in self.cancelled:
            raise PeerAnswerRefusedError(correlation_id)
        if correlation_id not in self.answers:
            self.answers[correlation_id] = answer
            if failed:
                self.failed.add(correlation_id)

    async def answer_failed(self, correlation_id: str) -> bool:
        return correlation_id in self.failed

    async def read_answer(self, correlation_id: str) -> str | None:
        raw = self.answers.get(correlation_id)
        return None if raw is None else wrap_peer_answer(raw)


class Resume:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, object], ...]] = []
        self.approvals: list[tuple[tuple[str, bool], ...]] = []
        self.then: list[TurnOutcome] = []  # outcomes to return before finishing

    async def execute(
        self,
        turn_id: TurnId,
        session: Any,
        profile_id: str,
        resolutions: Any,
        *,
        caller: Any,
        owner: Any = None,
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
    # C21. The progress step runs its real body (no DBOS context needed); with no publisher bound it
    # publishes nothing, and a test that wants the events binds a recorder on the dependencies.
    monkeypatch.setattr(
        turn_workflow, "_step_progress", inspect.unwrap(turn_workflow._step_progress)
    )
    # D95. The route step runs its real body too: with no `route_turn` bound it hands the request
    # back unchanged, and a test that wants routing binds one on the dependencies.
    monkeypatch.setattr(turn_workflow, "_step_route", inspect.unwrap(turn_workflow._step_route))
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


def ok(corr: str, text: str, *, kind: DelegationKind = DelegationKind.PEER) -> str:
    """The envelope the model must be handed for `corr` answered with `text` (C8)."""
    session = new_peer_session(
        TenantId("t"), request().session, key=peer_session_key(corr)
    ).session_id
    return PeerResult(
        peer=PEER,
        status=PeerStatus.OK,
        text=wrap_peer_answer(text),
        session=str(session),
        kind=kind,
    ).render()


def answer(corr: str, text: str, *, failed: bool = False) -> Callable[[World], Any]:
    def run(world: World) -> Any:
        world.mailbox.answers[corr] = text
        if failed:
            world.mailbox.failed.add(corr)
        return turn_workflow.PeerAnswer(turn_id=TurnId("turn-1"), correlation_id=corr)

    return run


def nobody_answers(advance: float) -> Callable[[World], Any]:
    def run(world: World) -> Any:
        world.clock[0] += advance
        return None

    return run


def asker_policy(**changes: Any) -> None:
    """Change the asking profile's `PeerPolicy` in the bound seat (after `install`)."""
    seat = turn_workflow._dependencies.peers  # noqa: SLF001 - the test seam
    assert seat is not None
    asker = seat.profiles[ASKER]
    seat.profiles[ASKER] = dataclasses.replace(
        asker, peers=dataclasses.replace(asker.peers, **changes)
    )


def asker_subagents(**changes: Any) -> None:
    """Change the asking profile's `SubagentPolicy` in the bound seat (after `install`). By
    default the asker may spawn the one peer profile; pass `enabled=False` or `agents=()` to take
    it back."""
    seat = turn_workflow._dependencies.peers  # noqa: SLF001 - the test seam
    assert seat is not None
    asker = seat.profiles[ASKER]
    values: dict[str, Any] = {"enabled": True, "agents": (AgentId(PEER),)}
    values.update(changes)
    seat.profiles[ASKER] = dataclasses.replace(asker, subagents=SubagentPolicy(**values))
    # R1: the worker consents to its spawner, so a test only takes the asker's side away.
    for agent in values["agents"]:
        worker = seat.profiles.get(str(agent))
        if worker is not None:
            seat.profiles[str(agent)] = dataclasses.replace(
                worker, subagents=_accepting(worker.subagents, AgentId(ASKER))
            )


def _accepting(policy: SubagentPolicy, spawner: AgentId) -> SubagentPolicy:
    return dataclasses.replace(policy, accept_from=(*policy.accept_from, spawner))


def callee_profile(**changes: Any) -> None:
    """Change the CALLEE profile (the peer the asker asks) in the bound seat (after `install`)."""
    seat = turn_workflow._dependencies.peers  # noqa: SLF001 - the test seam
    assert seat is not None
    seat.profiles[PEER] = dataclasses.replace(seat.profiles[PEER], **changes)
