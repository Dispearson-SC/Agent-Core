"""Nested peer asks (A -> B -> C) on a REAL DBOS and the REAL `PgAgentMailbox` (C1f, D32).

Phase:   Agent-comms v2, wave 1
Covers:  adapters/driven/peers/mailbox.py (`peer_messages.turn_id` as TEXT, migration 0029),
         adapters/driving/workflow/turn_workflow.py (`_cancel_callees` cascade)

WHAT IS BEING DEFENDED

    1. A CALLEE CAN ASK A THIRD AGENT. B's turn id is its workflow id `peer-<correlation id>`, which
       is no UUID. Its ask to C used to fail on INSERT (`peer_messages.turn_id` was a UUID column);
       now C's answer flows to B, B's reply flows to A, and each hop is exactly one row.

    2. GIVING UP ON B GIVES UP ON C. When A cancels, or its reply timeout fires, B's workflow is
       cancelled and so are the asks B made (rows and workflows), however deep the chain.

Only the model-facing seats (`StartTurn`, `ResumeTurn`) and the human gateway are in-memory
doubles; the mailbox is the Postgres adapter, the engine is DBOS with the production config.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, cast

import psycopg
import pytest

from agent_core import composition
from agent_core.adapters.driven.peers.mailbox import PgAgentMailbox, wrap_peer_answer
from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
from agent_core.adapters.driving.peers.worker import AskDisposition, dispatch_next_ask
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
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

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
_DATABASE = "agent_core_peer_nested_test"
_DEADLINE = 60.0
_POLL = 0.05

_A, _B, _C = "p-a", "p-b", "p-c"
_ASKING_TURN = TurnId("nested-asker-turn")
_APPROVAL_CALL = ToolCallId("call_Approve-C")


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _conninfo() -> str:
    head, _, _ = _ADMIN_CONNINFO.rpartition("/")
    return f"{head}/{_DATABASE}"


async def _fresh_databases() -> None:
    with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=5, autocommit=True) as admin:
        for database in (f"{_DATABASE}_dbos_sys", _DATABASE):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    await migrations.ensure_databases(
        _ADMIN_CONNINFO, app_database=_DATABASE, dbos_database=f"{_DATABASE}_dbos_sys"
    )
    await migrations.run_migrations(_conninfo())


class _Dbos:
    async def __aenter__(self) -> None:
        from dbos import DBOS, DBOSConfig

        await _fresh_databases()
        settings = composition.Settings(app_conninfo=_conninfo())
        DBOS(config=cast("DBOSConfig", dict(composition.dbos_config(settings))))
        DBOS.launch()

    async def __aexit__(self, *_: object) -> None:
        from dbos import DBOS

        DBOS.destroy()


def _profiles() -> dict[str, AgentProfile]:
    def side(me: str, *others: str) -> AgentProfile:
        return AgentProfile(
            id=me,
            persona=me,
            model="m",
            peers=PeerPolicy(
                enabled=True,
                peers=tuple(AgentRef(agent_id=AgentId(o), display_name=o) for o in others),
                max_hops=3,
                reply_timeout_seconds=600,
            ),
        )

    return {_A: side(_A, _B), _B: side(_B, _A, _C), _C: side(_C, _B)}


def _request() -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t")),
        caller=CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t")),
        profile_id=_A,
        input=UserInput(text="start"),
    )


class _Start:
    """A asks B, B asks C, C either answers at once or waits for its human."""

    def __init__(self, *, c_waits_for_a_human: bool) -> None:
        self.c_waits_for_a_human = c_waits_for_a_human

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        asks = {_A: _B, _B: _C}
        if request.profile_id in asks:
            pending = PendingRequest(
                kind=PendingKind.DELEGATION,
                tool_call_id=ToolCallId(f"call_{request.profile_id}"),
                tool_name="ask_peer",
                arguments={"target": asks[request.profile_id], "question": "pass it on"},
                reason="asking",
            )
            return TurnOutcome(turn_id=turn_id, pending=(pending,))
        if self.c_waits_for_a_human:
            approval = PendingRequest(
                kind=PendingKind.APPROVAL,
                tool_call_id=_APPROVAL_CALL,
                tool_name="freeze_account",
                arguments={},
                reason="a human must agree",
            )
            return TurnOutcome(turn_id=turn_id, pending=(approval,))
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="c answers"))


class _Resume:
    def __init__(self) -> None:
        self.payloads: dict[str, list[object]] = {}

    async def execute(
        self,
        turn_id: TurnId,
        session: SessionRef,
        profile_id: str,
        resolutions: tuple[Any, ...],
        *,
        caller: CallerIdentity,
    ) -> TurnOutcome:
        self.payloads.setdefault(profile_id, []).extend(r.payload for r in resolutions)
        text = {_A: "a done", _B: "b relays"}.get(profile_id, "?")
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=text))


class _Gateway:
    def __init__(self) -> None:
        self.published: list[TurnId] = []

    async def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:
        self.published.append(turn_id)


class _Channel:
    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        return None


@dataclass
class _World:
    mailbox: PgAgentMailbox
    start: _Start
    resume: _Resume = field(default_factory=_Resume)
    gateway: _Gateway = field(default_factory=_Gateway)


def _bind(monkeypatch: pytest.MonkeyPatch, *, c_waits_for_a_human: bool) -> _World:
    world = _World(
        mailbox=PgAgentMailbox(_conninfo()), start=_Start(c_waits_for_a_human=c_waits_for_a_human)
    )
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", world.start),
            channels=ChannelRegistry((("http", _Channel()),)),
            human_gateway=cast("Any", world.gateway),
            resume_turn=cast("Any", world.resume),
            peers=turn_workflow.PeerSeat(mailbox=world.mailbox, profiles=_profiles()),
        ),
    )
    return world


def _rows() -> dict[str, tuple[str, str, str | None]]:
    """correlation id -> (asking turn id, state, answer)."""
    with psycopg.connect(_conninfo()) as conn:
        found = conn.execute("SELECT correlation_id, turn_id, state, answer FROM peer_messages")
        return {str(c): (str(t), str(s), a) for c, t, s, a in found.fetchall()}


async def _until(predicate: Any, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + _DEADLINE
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(_POLL)
    raise AssertionError(f"timed out waiting for {what}")


async def _dispatch(world: _World, target: str) -> AskDisposition | None:
    report = await dispatch_next_ask(
        world.mailbox,
        turn_workflow.DurablePeerTurnStarter(),
        target=AgentId(target),
        wake=turn_workflow.signal_peer_answer,
    )
    return None if report is None else report.disposition


async def _status(workflow_id: str) -> str | None:
    from dbos import DBOS

    status = await DBOS.get_workflow_status_async(workflow_id)
    return None if status is None else status.status


async def _asker_result() -> TurnOutcome:
    from dbos import DBOS

    handle = await DBOS.retrieve_workflow_async(str(_ASKING_TURN))
    return cast("TurnOutcome", await asyncio.wait_for(handle.get_result(), _DEADLINE))


async def _run_to_the_third_agent(world: _World) -> tuple[str, str]:
    """A asks B, B starts and asks C, C is dispatched. Returns (A->B id, B->C id)."""
    await turn_workflow.enqueue_turn(_request(), turn_id=_ASKING_TURN)
    await _until(lambda: _rows(), "A's ask to reach the mailbox")
    assert await _dispatch(world, _B) is AskDisposition.STARTED
    await _until(lambda: len(_rows()) == 2, "B's ask to C to reach the mailbox")
    assert await _dispatch(world, _C) is AskDisposition.STARTED
    first = next(c for c, (turn, _, _) in _rows().items() if turn == str(_ASKING_TURN))
    second = next(c for c in _rows() if c != first)
    return first, second


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_nested_ask_is_answered_through_both_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _bind(monkeypatch, c_waits_for_a_human=False)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with _Dbos():
            seen["ids"] = await _run_to_the_third_agent(world)
            seen["outcome"] = await _asker_result()

    asyncio.run(drive())

    first, second = seen["ids"]
    rows = _rows()
    assert rows[second][0] == f"peer-{first}", "B's ask is filed under B's workflow id"
    assert rows[first][2] == "b relays" and rows[second][2] == "c answers"
    assert seen["outcome"].result.text == "a done"
    assert world.resume.payloads[_B] == [wrap_peer_answer("c answers")]
    assert world.resume.payloads[_A] == [wrap_peer_answer("b relays")]


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_cancelling_the_asker_cancels_the_third_agent_too(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _bind(monkeypatch, c_waits_for_a_human=True)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with _Dbos():
            seen["ids"] = await _run_to_the_third_agent(world)
            await _until(lambda: world.gateway.published, "C's approval request")
            seen["cancelled"] = await turn_workflow.cancel_turn(_ASKING_TURN, "operator cancel")
            first, second = seen["ids"]
            for name, corr in (("b", first), ("c", second)):
                seen[name] = await _status(turn_workflow.peer_workflow_id(corr))
            # C's human approves after the fact: nothing may be written or woken
            await turn_workflow.signal_decision(
                TurnId(turn_workflow.peer_workflow_id(second)), _APPROVAL_CALL, True, None
            )
            await asyncio.sleep(1.0)

    asyncio.run(drive())

    first, second = seen["ids"]
    assert seen["cancelled"] == (first, second)
    assert seen["b"] == "CANCELLED" and seen["c"] == "CANCELLED"
    rows = _rows()
    assert rows[first][1] == "cancelled" and rows[second][1] == "cancelled"
    assert rows[first][2] is None and rows[second][2] is None, "no answer was written"


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_askers_reply_timeout_cancels_the_third_agent_through_the_recorded_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _bind(monkeypatch, c_waits_for_a_human=True)

    async def timeout(request: TurnRequest) -> int:
        return 1 if request.profile_id == _A else 600

    monkeypatch.setattr(turn_workflow, "_step_peer_reply_timeout", timeout)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with _Dbos():
            seen["ids"] = await _run_to_the_third_agent(world)
            seen["outcome"] = await _asker_result()
            first, second = seen["ids"]
            seen["b"] = await _status(turn_workflow.peer_workflow_id(first))
            seen["c"] = await _status(turn_workflow.peer_workflow_id(second))

    asyncio.run(drive())

    first, second = seen["ids"]
    assert seen["outcome"].result.text == "a done", "A went on with the timeout text"
    assert seen["b"] == "CANCELLED" and seen["c"] == "CANCELLED"
    rows = _rows()
    assert rows[first][1] == "cancelled" and rows[second][1] == "cancelled"
