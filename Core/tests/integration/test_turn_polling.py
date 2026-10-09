"""One id from `POST /turns` to the audit trail, and the human decision route.

Phase:   F0 (the pull half of the 202) / F3 (the decision route)
Tasks:   docs/TASKS.md#t-f0-06, docs/TASKS.md#t-f3-11
Covers:  adapters/driving/http/routes.py            - GET /turns/{turn_id}, POST /decisions
         adapters/driving/workflow/turn_workflow.py - the id the turn is filed under, and
                                                      the paired send

WHAT IS BEING DEFENDED - TWO IDENTIFIERS WEARING ONE TYPE NAME
    `POST /turns` hands a caller an id and promises the answer will be there later.
    Everything the turn writes - the audit rows, the correlation the human replies on -
    is filed under the domain `TurnId` the workflow works with. If those are two
    different values, every one of those promises is broken SILENTLY: the poll returns
    "unknown turn" forever, the audit trail cannot be joined to the request that produced
    it, and `DBOS.send_async(destination_id=turn_id)` addresses a workflow that does not
    exist, so an approval is recorded and nothing ever wakes up.

    None of that raises. A lookup that returns nothing looks exactly like a turn that is
    still running, which is why this is asserted rather than reasoned about.

WHY THIS IS AN INTEGRATION TEST
    The join being asserted is made by DBOS itself: the id the edge pins with
    `SetWorkflowID` has to be the id the workflow body reads back, through a coalescing
    window that may hand back somebody else's handle. A fake queue would assert that the
    fake honours the options it was given. Only a real DBOS over a real Postgres can say
    whether the id survives the window, the partition and the enqueue.

    It skips cleanly when no Postgres is reachable, exactly like its neighbours here.

WHY THE DECISION ROUTE IS PROVED BY HOLDING THE RESUME STEP
    "The route never blocks" cannot be proved with a stopwatch - a fast machine passes a
    blocking route too. So `_step_resume` is held open until the test releases it, and
    the assertion is causal: the response arrived while the turn was demonstrably still
    inside the step it was woken into. The hold is bounded, so a route that DOES block
    fails with a sentence instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import psycopg
import pytest

from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
from agent_core.adapters.driving.http import routes
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.application.decide_approval import DecideApproval
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionRef,
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

# Its own DBOS system database, for the reason test_coalescing.py gives: a failed run here
# can be dropped without touching anything another test owns.
_DBOS_DATABASE = "agent_core_polling_test"

# Short: every test below waits on a causal event, never on this. It only has to be wide
# enough that three messages could not accidentally coalesce across separate tests.
_WINDOW_SECONDS = 0.2

# Turns a hang into a readable failure. Never a tolerance an assertion depends on.
_DEADLINE_SECONDS = 60.0

# How long `_step_resume` holds itself open when the test has not released it. A route
# that blocked on the turn would otherwise deadlock against this hold; bounded, it fails
# on the assertion below instead of hanging the suite.
_HOLD_SECONDS = 5.0

_CORRELATION_ID = "corr-under-test"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _dbos_conninfo() -> str:
    head, _, _ = _ADMIN_CONNINFO.rpartition("/")
    return f"{head}/{_DBOS_DATABASE}"


def _drop_databases() -> None:
    """Start every test from an empty DBOS system table, and why that is not paranoia.

    `agent-core-turns` is partitioned with `partition_concurrency=1`, so ONE workflow left
    PENDING in a session's partition occupies that partition's only slot. A run killed
    mid-turn - a session limit, a Ctrl-C, a crashed worker - leaves exactly that row, and
    it is stamped with the application version that produced it, so the next process never
    recovers it and never releases the slot. Every later turn for that session is accepted,
    enqueued, and sits at ENQUEUED forever.

    Nothing raises. `POST /turns` still answers 202 with a well-formed id, and the failure
    reads as "the turn never started" sixty seconds later - which is indistinguishable from
    the id defect this file exists to catch. So the state is destroyed rather than reused:
    these two databases are named for this file and nothing else reads them.
    """
    with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=5, autocommit=True) as admin:
        for database in (f"{_DBOS_DATABASE}_dbos_sys", _DBOS_DATABASE):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


class _Dbos:
    """A launched DBOS for the length of one test, destroyed however the test ends."""

    def __init__(self, name: str) -> None:
        self._name = name

    async def __aenter__(self) -> None:
        from dbos import DBOS, DBOSConfig

        _drop_databases()
        config: DBOSConfig = {"name": self._name, "database_url": _dbos_conninfo()}
        DBOS(config=config)
        DBOS.launch()

    async def __aexit__(self, *_: object) -> None:
        from dbos import DBOS

        DBOS.destroy()


class _DeliveredChannel:
    """Records that a finished turn left the building, and produces nothing.

    `_step_deliver` raises `UnknownChannelError` on a miss, so a channel has to be
    registered under the id `routes.py` stamps on an HTTP turn or every run here dies at
    the last step for a reason no assertion is about.
    """

    def __init__(self) -> None:
        self.delivered = threading.Event()
        self.sent: list[OutboundMessage] = []

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        self.sent.append(message)
        self.delivered.set()


@dataclass
class _RecordingStartTurn:
    """`StartTurn`, as far as this file needs it: what id was the turn filed under?

    It stands in for the whole application layer on purpose. The real `StartTurn` writes
    the audit rows and persists the outcome under the `turn_id` its caller handed it, so
    the id THIS fake is handed is, by construction, the id every audit row would carry.
    Recording it is the audit assertion, without needing a database of rows to join.
    """

    outcome_for: Any = None
    filed_under: list[TurnId] = field(default_factory=list)
    outcomes: dict[TurnId, TurnOutcome] = field(default_factory=dict)
    started: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        outcome = (
            self.outcome_for(turn_id)
            if self.outcome_for is not None
            else TurnOutcome(turn_id=turn_id, result=TurnResult(text="done"))
        )
        with self._lock:
            self.filed_under.append(turn_id)
            # Standing in for `ConversationStore.append_outcome`, which the real use case
            # calls before it returns - so by the time a caller can poll, the answer is
            # already where the reader below looks for it (composition.py, PullModeChannel).
            self.outcomes[turn_id] = outcome
        self.started.set()
        return outcome


@dataclass
class _Buffer:
    """The pending-input sink the coalescing starter appends to. Nothing here drains it."""

    appended: list[tuple[SessionRef, UserInput]] = field(default_factory=list)

    async def append(self, session: SessionRef, message: UserInput) -> None:
        self.appended.append((session, message))


@dataclass
class _RecordingGateway:
    """`HumanGateway`, reduced to the two things the decision route needs from it.

    `publish` mints ONE correlation handle for the request it was asked about and remembers
    which turn it belongs to - which is the whole of what the real adapter's table does.
    The test reads the handle back out afterwards, exactly as a human would read it out of
    the message they were sent.
    """

    published: threading.Event = field(default_factory=threading.Event)
    correlations: dict[str, tuple[TurnId, ToolCallId]] = field(default_factory=dict)

    async def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:
        for request in requests:
            self.correlations[_CORRELATION_ID] = (turn_id, request.tool_call_id)
        self.published.set()

    async def correlate(self, correlation_id: str) -> tuple[TurnId, ToolCallId] | None:
        return self.correlations.get(correlation_id)


@dataclass
class _RecordingAudit:
    """`AuditSink`, reduced to the one member `DecideApproval` reaches for."""

    decisions: list[tuple[TurnId, ToolCallId, str, bool, str | None]] = field(
        default_factory=list
    )

    async def record_human_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        approved: bool,
        note: str | None = None,
    ) -> None:
        self.decisions.append((turn_id, tool_call_id, subject_id, approved, note))


def _wired(
    start_turn: object,
    channel: _DeliveredChannel,
    gateway: object | None = None,
) -> turn_workflow.TurnWorkflowDependencies:
    return turn_workflow.TurnWorkflowDependencies(
        start_turn=cast("Any", start_turn),
        channels=ChannelRegistry((("http", channel),)),
        human_gateway=cast("Any", gateway),
    )


def _headers(subject: str = "u-1", tenant: str = "t-1") -> dict[str, str]:
    return {"X-Subject-Id": subject, "X-Tenant-Id": tenant}


def _body(session: str = "s-1") -> dict[str, str]:
    return {"session_id": session, "profile_id": "p-1", "text": "hola"}


def _lookup_seat() -> bool:
    return "lookup_turn" in inspect.signature(routes.create_app).parameters


def _decide_seat() -> bool:
    return "decide" in inspect.signature(routes.create_app).parameters


def _suspended(turn_id: TurnId) -> TurnOutcome:
    return TurnOutcome(
        turn_id=turn_id,
        pending=(
            PendingRequest(
                kind=PendingKind.APPROVAL,
                tool_call_id=ToolCallId("call-1"),
                tool_name="refund",
                arguments={"amount": 10},
                reason="a refund over the limit needs a human",
            ),
        ),
    )


async def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent-core.test"
    )


async def _await(event: threading.Event, what: str) -> None:
    """Wait for a causal signal WITHOUT blocking the caller's event loop.

    `threading.Event.wait` called straight from a coroutine parks the whole loop, and the
    ASGI app under test - the one that enqueues the next turn - lives on it. Handing the
    wait to a thread keeps the loop free, so what is being waited for can actually happen.
    """
    assert await asyncio.to_thread(event.wait, _DEADLINE_SECONDS), (
        f"{what} never happened within {_DEADLINE_SECONDS}s. Nothing below this line can "
        "assert anything about an id that was never produced."
    )


@pytest.mark.phase("F0")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_id_post_returns_is_the_id_the_turn_is_filed_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f0-06, the half that nothing joined.

    The caller is handed an id and told to come back with it. The workflow files every
    audit row, every correlation and every stored outcome under the domain `TurnId` it
    works with. Those must be ONE value.

    Two identifiers under one type name is not a cosmetic problem: the poll returns
    nothing forever, and it looks exactly like a turn that is still running.
    """
    started = _RecordingStartTurn()
    channel = _DeliveredChannel()
    monkeypatch.setattr(turn_workflow, "_dependencies", _wired(started, channel))

    app = routes.create_app(
        start_turn=routes.coalescing_turn_starter(
            buffer=_Buffer(), window_seconds=_WINDOW_SECONDS
        )
    )

    async def drive() -> str:
        async with _Dbos("agent-core-polling-test"):
            async with await _client(app) as client:
                response = await client.post("/turns", json=_body(), headers=_headers())
            assert response.status_code == 202, response.text
            posted = str(response.json()["turn_id"])
            await _await(started.started, "the turn never reached StartTurn")
            await _await(channel.delivered, "the turn never finished")
            return posted

    posted = asyncio.run(drive())

    assert started.filed_under, "no turn ran, so there is no id to compare against."
    filed = str(started.filed_under[0])

    assert posted == filed, (
        f"POST /turns handed the caller {posted!r} and the turn was filed under {filed!r}. "
        "Those are two identifiers wearing one type name: every audit row, the "
        "human_requests correlation and DecideApproval's DBOS.send_async destination all "
        "use the second one, so a caller polling the first gets nothing back forever and "
        "an approval addressed to it wakes no workflow. docs/TASKS.md#t-f0-06"
    )


@pytest.mark.phase("F0")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_get_turns_answers_on_the_id_post_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f0-06 - the pull half of D23/GAPS A4's 202-plus-poll.

    The `http` channel's `send` is a deliberate no-op (composition.py, `PullModeChannel`)
    BECAUSE the answer is retrieved here. Without this route the no-op is simply a turn
    that reaches nobody, and every HTTP turn since wave 11 has been exactly that.
    """
    assert _lookup_seat(), (
        "routes.create_app has no lookup_turn seat, so there is no GET /turns/{turn_id}. "
        "The `http` channel's send is a no-op because the answer is meant to be RETRIEVED "
        "(composition.py, PullModeChannel); with no reader, every HTTP turn is answered "
        "into a void. docs/TASKS.md#t-f0-06"
    )

    started = _RecordingStartTurn()
    channel = _DeliveredChannel()
    monkeypatch.setattr(turn_workflow, "_dependencies", _wired(started, channel))

    async def lookup(turn_id: TurnId, caller: CallerIdentity) -> Any:
        outcome = started.outcomes.get(turn_id)
        if outcome is None:
            return None
        return routes.TurnView(
            turn_id=turn_id,
            status="finished" if outcome.result is not None else "waiting",
            text=outcome.result.text if outcome.result is not None else None,
        )

    app = routes.create_app(
        start_turn=routes.coalescing_turn_starter(
            buffer=_Buffer(), window_seconds=_WINDOW_SECONDS
        ),
        lookup_turn=lookup,
    )

    async def drive() -> tuple[str, httpx.Response, httpx.Response]:
        async with _Dbos("agent-core-polling-test"):
            async with await _client(app) as client:
                accepted = await client.post("/turns", json=_body(), headers=_headers())
                assert accepted.status_code == 202, accepted.text
                posted = str(accepted.json()["turn_id"])
                await _await(started.started, "the turn never reached StartTurn")
                await _await(channel.delivered, "the turn never finished")
                polled = await client.get(f"/turns/{posted}", headers=_headers())
                missing = await client.get("/turns/not-a-turn", headers=_headers())
            return posted, polled, missing

    posted, polled, missing = asyncio.run(drive())

    assert polled.status_code == 200, (
        f"GET /turns/{posted} answered {polled.status_code}: {polled.text}. The id POST "
        "handed out has to be the id this route resolves, or the 202 is a promise nothing "
        "keeps. docs/TASKS.md#t-f0-06"
    )
    answered = polled.json()
    assert str(answered["turn_id"]) == posted, (
        f"GET answered on {answered['turn_id']!r} for a poll of {posted!r}."
    )
    assert answered["status"] == "finished" and answered["text"] == "done", (
        f"GET returned {answered!r}; the stored outcome was 'done'. The pull half of the "
        "202 has to hand back the answer that was persisted, not an empty envelope."
    )
    assert str(started.filed_under[0]) == posted, (
        "GET answered on an id the turn was not filed under, so the route and the audit "
        "trail disagree about which turn this was."
    )
    assert missing.status_code == 404, (
        f"GET on an unknown turn id answered {missing.status_code}. An unknown handle is "
        "404 and never a guess: resolving a stray id into some other turn hands one "
        "caller another caller's answer."
    )


@pytest.mark.phase("F3")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_post_decisions_resolves_a_correlation_and_wakes_the_turn_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f3-11, and the reason it could not be built before t-f0-06.

    `DecideApproval` signals on the DOMAIN turn id. `DBOS.send_async` addresses a WORKFLOW
    id. While those were different values the send went to a workflow that does not exist,
    which raises nothing anybody sees: the audit row is written, the caller is told the
    decision was accepted, and the turn sleeps for three days.

    THE NON-BLOCKING HALF IS PROVED CAUSALLY, NOT BY A CLOCK
        `_step_resume` is held open. The response comes back while the woken turn is
        demonstrably still inside it, so the route provably did not wait for the turn.
        A stopwatch would pass a blocking route on a fast machine.
    """
    assert _decide_seat(), (
        "routes.create_app has no decide seat, so there is no POST /decisions/{corr_id}. "
        "Every turn that suspends on a human has no route back and expires after three "
        "days. docs/TASKS.md#t-f3-11"
    )
    signal = getattr(turn_workflow, "signal_decision", None)
    assert signal is not None, (
        "turn_workflow exports no send-side counterpart to run_turn_workflow's "
        "DBOS.recv_async, so composition.py's DecisionSignal seat has nothing to bind "
        "(it raises NotImplementedError today). docs/TASKS.md#t-f3-11"
    )

    entered = threading.Event()
    release = threading.Event()
    received: list[object] = []

    started = _RecordingStartTurn(outcome_for=_suspended)
    channel = _DeliveredChannel()
    gateway = _RecordingGateway()
    audit = _RecordingAudit()
    monkeypatch.setattr(turn_workflow, "_dependencies", _wired(started, channel, gateway))

    async def resume(turn_id: TurnId, request: TurnRequest, answer: object) -> TurnOutcome:
        received.append(answer)
        entered.set()
        await asyncio.to_thread(release.wait, _HOLD_SECONDS)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="resumed"))

    monkeypatch.setattr(turn_workflow, "_step_resume", resume)

    decide = DecideApproval(
        gateway=cast("Any", gateway), audit=cast("Any", audit), signal=signal
    )
    app = routes.create_app(
        start_turn=routes.coalescing_turn_starter(
            buffer=_Buffer(), window_seconds=_WINDOW_SECONDS
        ),
        decide=decide,
    )

    async def drive() -> tuple[str, httpx.Response, bool]:
        async with _Dbos("agent-core-polling-test"):
            async with await _client(app) as client:
                accepted = await client.post("/turns", json=_body(), headers=_headers())
                assert accepted.status_code == 202, accepted.text
                posted = str(accepted.json()["turn_id"])
                await _await(gateway.published, "the turn never asked a human")

                answered = await client.post(
                    f"/decisions/{_CORRELATION_ID}",
                    json={"approved": True, "note": "go ahead"},
                    # A DIFFERENT subject from the one that started the turn: an approval
                    # from the requester is D25's four-eyes failure, not this test's subject.
                    headers=_headers(subject="u-2"),
                )
                # Read BEFORE the hold is released: if the route waited for the turn, the
                # turn would already have been delivered by the time this line runs.
                still_running = not channel.delivered.is_set()
            release.set()
            await _await(entered, "the decision never reached the waiting workflow")
            await _await(channel.delivered, "the woken turn never finished")
            return posted, answered, still_running

    posted, answered, still_running = asyncio.run(drive())

    assert answered.status_code == 202, (
        f"POST /decisions answered {answered.status_code}: {answered.text}. A resolved "
        "correlation is accepted and the turn is woken; the route never waits for it."
    )
    assert received, (
        "the decision never reached the waiting workflow: DBOS.recv_async is still "
        "blocked. DecideApproval signals on the domain turn id and DBOS.send_async "
        "addresses a workflow id - while those differ the send reaches nothing, the audit "
        "row still lands, and the caller is told it worked. docs/TASKS.md#t-f3-11"
    )
    assert still_running, (
        "POST /decisions did not come back until the turn had finished. t-f0-03's rule for "
        "the whole HTTP surface is that a route never waits for a turn: this one held its "
        "connection for the length of a model call, and it dies on the next deploy taking "
        "the turn with it."
    )
    assert audit.decisions and audit.decisions[0][0] == TurnId(posted), (
        f"the human decision was filed under {audit.decisions and audit.decisions[0][0]!r} "
        f"while the caller polls {posted!r}."
    )
    assert audit.decisions[0][2] == "u-2", (
        "the decision was recorded against a subject the body supplied rather than the "
        "authenticated one. D25's four-eyes rule compares subject ids, so a body-supplied "
        "approver lets the requester approve their own request by typing another name."
    )


@pytest.mark.phase("F3")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_an_unknown_correlation_is_404_and_never_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray reply must not be routed into the nearest turn.

    `HumanGateway.correlate` returns None for an unknown or expired handle and
    `DecideApproval` turns that into `UnknownCorrelationError`. The route maps it to 404
    and stops: guessing which turn a stray reply belongs to approves an action nobody
    approved, and it looks like success in the log.
    """
    assert _decide_seat(), "routes.create_app has no decide seat; see the test above."

    gateway = _RecordingGateway()
    audit = _RecordingAudit()
    decide = DecideApproval(
        gateway=cast("Any", gateway),
        audit=cast("Any", audit),
        signal=cast("Any", turn_workflow.signal_decision),
    )
    app = routes.create_app(
        start_turn=cast("Any", None),
        decide=decide,
    )

    async def drive() -> httpx.Response:
        async with await _client(app) as client:
            return await client.post(
                "/decisions/nobody-asked",
                json={"approved": True},
                headers=_headers(subject="u-2"),
            )

    response = asyncio.run(drive())

    assert response.status_code == 404, (
        f"an unresolvable correlation answered {response.status_code}: {response.text}. "
        "It must be 404 - never a 500, and never a guess at which turn it meant."
    )
    assert not audit.decisions, (
        "an unresolvable correlation still recorded a human decision, so the audit trail "
        "now carries an approval for a turn nobody can name."
    )
