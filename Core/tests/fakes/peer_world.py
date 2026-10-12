"""A real DBOS and the REAL `PgAgentMailbox` with in-memory model-side doubles.

Shared by the integration tests that need the Postgres mailbox and the production DBOS
configuration together (nested asks, version bumps, restarts, the budget). Only the seats a model
or a person sits behind are doubles: `StartTurn`, `ResumeTurn`, the human gateway and the channel.

    world = bind(monkeypatch)
    async with Dbos():
        await turn_workflow.enqueue_turn(request(), turn_id=ASKING_TURN)
        ...

Three agents, A -> B -> C. A and B ask (an `ask_peer` call each); C answers at once, or waits
for a person when `Start.c_waits_for_a_human` is set. A test mutates the doubles between phases.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import psycopg
import pytest

from agent_core import composition
from agent_core.adapters.driven.peers.mailbox import PgAgentMailbox
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

ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
DEADLINE = 60.0
POLL = 0.05

A, B, C = "p-a", "p-b", "p-c"
ASKING_TURN = TurnId("world-asker-turn")
APPROVAL_CALL = ToolCallId("call_Approve-C")


def postgres_reachable() -> bool:
    try:
        with psycopg.connect(ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def conninfo(database: str) -> str:
    head, _, _ = ADMIN_CONNINFO.rpartition("/")
    return f"{head}/{database}"


async def fresh_databases(database: str) -> None:
    """Drop and re-create the app database (migrated) and DBOS's system database."""
    with psycopg.connect(ADMIN_CONNINFO, connect_timeout=5, autocommit=True) as admin:
        for name in (f"{database}_dbos_sys", database):
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await migrations.ensure_databases(
        ADMIN_CONNINFO, app_database=database, dbos_database=f"{database}_dbos_sys"
    )
    await migrations.run_migrations(conninfo(database))


class Dbos:
    """The production DBOS configuration, launched. `drop=False` INHERITS what a killed process
    left (databases, workflows), exactly as the next process to start would; `application_version`
    overrides the pinned version to simulate a deploy."""

    def __init__(
        self, database: str, *, drop: bool = True, application_version: str | None = None
    ) -> None:
        self._database = database
        self._drop = drop
        self._version = application_version

    async def __aenter__(self) -> None:
        from dbos import DBOS, DBOSConfig

        if self._drop:
            await fresh_databases(self._database)
        fields: dict[str, Any] = {"app_conninfo": conninfo(self._database)}
        if self._version is not None:
            fields["dbos_application_version"] = self._version
        settings = composition.Settings(**fields)
        DBOS(config=cast("DBOSConfig", dict(composition.dbos_config(settings))))
        DBOS.launch()

    async def __aexit__(self, *_: object) -> None:
        from dbos import DBOS

        DBOS.destroy()


def profiles() -> dict[str, AgentProfile]:
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

    return {A: side(A, B), B: side(B, A, C), C: side(C, B)}


def request(profile: str = A) -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t")),
        caller=CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t")),
        profile_id=profile,
        input=UserInput(text="start"),
    )


class Start:
    """A asks B, B asks C (unless `b_answers_directly`), C answers or waits for its human."""

    def __init__(self, *, c_waits_for_a_human: bool = False) -> None:
        self.c_waits_for_a_human = c_waits_for_a_human
        self.b_answers_directly = False
        self.requests: list[TurnRequest] = []

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        self.requests.append(request)
        asks = {A: B} if self.b_answers_directly else {A: B, B: C}
        if request.profile_id in asks:
            pending = PendingRequest(
                kind=PendingKind.DELEGATION,
                tool_call_id=ToolCallId(f"call_{request.profile_id}"),
                tool_name="ask_peer",
                arguments={"target": asks[request.profile_id], "question": "pass it on"},
                reason="asking",
            )
            return TurnOutcome(turn_id=turn_id, pending=(pending,))
        if request.profile_id == C and self.c_waits_for_a_human:
            approval = PendingRequest(
                kind=PendingKind.APPROVAL,
                tool_call_id=APPROVAL_CALL,
                tool_name="freeze_account",
                arguments={},
                reason="a human must agree",
            )
            return TurnOutcome(turn_id=turn_id, pending=(approval,))
        text = "b answers" if request.profile_id == B else "c answers"
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=text))

    def starts(self, profile: str) -> int:
        return sum(1 for r in self.requests if r.profile_id == profile)


class Resume:
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
        text = {A: "a done", B: "b relays"}.get(profile_id, "c finished")
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=text))


class Gateway:
    def __init__(self) -> None:
        self.published: list[TurnId] = []

    async def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:
        self.published.append(turn_id)


class Channel:
    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        return None


@dataclass
class World:
    database: str
    mailbox: PgAgentMailbox
    start: Start
    resume: Resume = field(default_factory=Resume)
    gateway: Gateway = field(default_factory=Gateway)


def bind(
    monkeypatch: pytest.MonkeyPatch, database: str, *, c_waits_for_a_human: bool = False
) -> World:
    """Bind the doubles and the Postgres mailbox as the workflow's dependencies."""
    world = World(
        database=database,
        mailbox=PgAgentMailbox(conninfo(database)),
        start=Start(c_waits_for_a_human=c_waits_for_a_human),
    )
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", world.start),
            channels=ChannelRegistry((("http", Channel()),)),
            human_gateway=cast("Any", world.gateway),
            resume_turn=cast("Any", world.resume),
            peers=turn_workflow.PeerSeat(mailbox=world.mailbox, profiles=profiles()),
        ),
    )
    return world


def rows(database: str) -> dict[str, tuple[str, str, str | None]]:
    """correlation id -> (asking turn id, state, answer)."""
    with psycopg.connect(conninfo(database)) as conn:
        found = conn.execute("SELECT correlation_id, turn_id, state, answer FROM peer_messages")
        return {str(c): (str(t), str(s), a) for c, t, s, a in found.fetchall()}


async def until(predicate: Any, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + DEADLINE
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(POLL)
    raise AssertionError(f"timed out waiting for {what}")


async def dispatch(world: World, target: str) -> AskDisposition | None:
    report = await dispatch_next_ask(
        world.mailbox,
        turn_workflow.DurablePeerTurnStarter(),
        target=AgentId(target),
        wake=turn_workflow.signal_peer_answer,
    )
    return None if report is None else report.disposition


async def status(workflow_id: str) -> str | None:
    from dbos import DBOS

    found = await DBOS.get_workflow_status_async(workflow_id)
    return None if found is None else found.status


async def result_of(workflow_id: str) -> TurnOutcome:
    from dbos import DBOS

    handle = await DBOS.retrieve_workflow_async(workflow_id)
    return cast("TurnOutcome", await asyncio.wait_for(handle.get_result(), DEADLINE))


async def run_to_the_third_agent(world: World) -> tuple[str, str]:
    """A asks B, B starts and asks C, C is dispatched. Returns (A->B id, B->C id)."""
    await turn_workflow.enqueue_turn(request(), turn_id=ASKING_TURN)
    await until(lambda: rows(world.database), "A's ask to reach the mailbox")
    assert await dispatch(world, B) is AskDisposition.STARTED
    await until(lambda: len(rows(world.database)) == 2, "B's ask to C to reach the mailbox")
    assert await dispatch(world, C) is AskDisposition.STARTED
    found = rows(world.database)
    first = next(c for c, (turn, _, _) in found.items() if turn == str(ASKING_TURN))
    second = next(c for c in found if c != first)
    return first, second


async def _writes_nothing(job: turn_workflow.PeerTurnJob, text: str) -> None:
    return None


async def let_the_dead_settle() -> None:
    """After a destroy, tasks of the dead engine are still on this event loop and unwind on its
    next yield. Run that unwinding inside `dying_process`, before another engine is launched on
    the same DBOS singleton, or they would write through it."""
    await asyncio.sleep(1.0)


@contextmanager
def dying_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make the engine's shutdown look like a kill: a workflow torn down mid-wait must not get to
    record a "failed" answer on its row on the way out (a killed process writes nothing). Wrap the
    phase that ends in a destroy: `with dying_process(monkeypatch): ...`."""
    with monkeypatch.context() as patch:
        patch.setattr(turn_workflow, "_close_peer_ask", _writes_nothing)
        yield
