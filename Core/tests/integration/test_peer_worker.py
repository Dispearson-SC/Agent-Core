"""Integration tests for the peer worker - the consumer the queue never had.

Phase:   F11
Tasks:   docs/TASKS.md#t-f11-43
Covers:  adapters/driving/peers/worker.py
         adapters/driven/peers/mailbox.py (the answering half of it)

WHAT IS BEING DEFENDED

    1. THE ASK IS ACTUALLY RUN. `PgAgentMailbox.claim_next` has an exactly-once claim with
       eight concurrent workers behind it and had no consumer at all: an ask was enqueued
       durably and then waited forever. The first test drives a real claim off a real queue
       and asserts the answer lands back on the row the asking turn holds a handle to.

    2. IT RUNS AS THE TARGET AGENT, NOT AS THE ASKER. B's profile, B's toolset, B's policy,
       B's budget. A turn run under A's identity is a privilege escalation dressed as a
       question - A asks B, B has tools A does not, and the audit row would name A while
       A's permissions were never the ones consulted. The assertion is on the `TurnRequest`
       the worker built: `profile_id` is the TARGET, the caller is a `CallerIdentity` on the
       peer channel, and the session is a fresh one rather than the asking conversation's.

    3. A SUSPENDED PEER TURN IS NOT ANSWERED. B may suspend on its own human (the shipped
       `billing_specialist` needs approval before it freezes an account). The worker must
       not invent an answer for a turn that has not produced one, and must not hold the
       claim open waiting - which is the whole reason `ask` and `answer` are separated by a
       durable queue instead of by a function call.

    4. THE ANSWER COMES BACK AS UNTRUSTED CONTENT. CLAUDE.md non-negotiable #10. The worker
       writes the peer's bytes verbatim and `read_answer` wraps them; a worker that wrapped
       on write would double-wrap, and a model shown a nested boundary cannot tell which
       one is real.

    5. THE HOP TRAVELS. A -> B -> A cannot be caught by anything either side counts
       locally, so the count carried on the claimed row has to reach the turn the worker
       starts. A worker that dropped it would reset the cycle at every hop, and a limit
       that resets is not a limit.

The database tests skip cleanly without a reachable Postgres, the same pattern as
test_peer_mailbox.py. The polling test touches no database at all.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from dataclasses import dataclass, field

import psycopg
import pytest

from agent_core.adapters.driven.agent_pydantic.runner import UNTRUSTED_OPEN
from agent_core.adapters.driven.peers.mailbox import PeerAsk, PgAgentMailbox
from agent_core.adapters.driven.persistence_pg import migrations, peers_migration
from agent_core.adapters.driving.peers.worker import (
    PEER_CHANNEL,
    AskDisposition,
    PeerTurnReport,
    answer_next_ask,
    run_peer_worker,
)
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
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
)

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_SUPPORT = AgentId("support_triage")
_BILLING = AgentId("billing_specialist")

_SUPPORT_POLICY = PeerPolicy(
    enabled=True,
    peers=(AgentRef(agent_id=_BILLING, display_name="Billing specialist"),),
    max_hops=1,
)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo(app_db: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)


def _migrated_conninfo() -> str:
    """A migrated app database for this module. Idempotent, so every test may call it."""
    app_db = "agent_core_peer_worker_test"
    dbos_db = "agent_core_peer_worker_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    app_conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(app_conninfo))
    asyncio.run(peers_migration.apply_peer_messages_migration(app_conninfo))
    return app_conninfo


def _empty_the_queue(conninfo: str) -> None:
    """Start from an empty queue.

    `claim_next` takes the OLDEST queued ask, so a row left behind by an earlier run of
    this module - or by an earlier test in it - is the one a later test would claim. The
    database is this module's own, created by `_migrated_conninfo` above, so emptying it
    touches nothing else.
    """
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("DELETE FROM peer_messages")


def _asking_session() -> SessionRef:
    """The conversation A is holding open while it waits for B."""
    return SessionRef(session_id=SessionId(f"support-{uuid.uuid4()}"), tenant_id=TenantId("t-1"))


@dataclass
class _RecordingRunner:
    """A peer turn that records what it was asked to be, and answers what it was told to.

    The worker's whole security claim is in the `TurnRequest` it builds, so the assertions
    are on `seen` rather than on anything the model would have done with it.
    """

    outcome: TurnOutcome | None = None
    text: str = "the charge on the 3rd is the annual renewal."
    seen: list[tuple[TurnId, TurnRequest, int]] = field(default_factory=list)

    async def execute(
        self, turn_id: TurnId, request: TurnRequest, *, hop: int
    ) -> TurnOutcome:
        self.seen.append((turn_id, request, hop))
        if self.outcome is not None:
            return self.outcome
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=self.text))


def _suspended(turn_id: TurnId) -> TurnOutcome:
    """What B returns when it has to ask a human before it can answer."""
    return TurnOutcome(
        turn_id=turn_id,
        pending=(
            PendingRequest(
                kind=PendingKind.APPROVAL,
                tool_call_id=ToolCallId("call-1"),
                tool_name="freeze_account",
                arguments={},
                reason="Freezing an account always needs a human.",
            ),
        ),
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_worker_claims_an_ask_runs_it_as_the_target_and_answers_it() -> None:
    """The loop closes: a durable ask becomes a turn, and the answer lands on the row.

    Everything the asking side has is a correlation id. Until this passes, that handle
    redeems to None forever however healthy the queue looks.
    """
    conninfo = _migrated_conninfo()
    _empty_the_queue(conninfo)
    session = _asking_session()
    turn_id = TurnId(str(uuid.uuid4()))
    question = f"what was charged on the 3rd? [{uuid.uuid4()}]"

    mailbox = PgAgentMailbox(conninfo)
    correlation_id = asyncio.run(
        mailbox.ask(
            _SUPPORT_POLICY,
            _BILLING,
            question,
            from_session=session,
            turn_id=turn_id,
            hop=1,
        )
    )

    runner = _RecordingRunner()
    report = asyncio.run(answer_next_ask(mailbox, runner, target=_BILLING))

    assert isinstance(report, PeerTurnReport)
    assert report.disposition is AskDisposition.ANSWERED
    assert report.correlation_id == correlation_id
    assert report.target == _BILLING

    # 2 - THE TURN RAN AS B, NOT AS A.
    assert len(runner.seen) == 1
    _, request, hop = runner.seen[0]
    assert request.profile_id == _BILLING
    assert isinstance(request.caller, CallerIdentity)
    assert request.caller.channel == PEER_CHANNEL
    assert request.caller.tenant_id == session.tenant_id
    assert request.input.text == question

    # A FRESH SESSION, for cron.py's reason: inheriting the asking conversation would let
    # one customer's history answer another customer's question.
    assert request.session.session_id != session.session_id
    assert request.session.tenant_id == session.tenant_id

    # 5 - THE HOP TRAVELLED. The row carried 1; the turn was started with 1.
    assert hop == 1

    # 1 and 4 - the answer is on the row the asking turn is waiting on, wrapped.
    answer = asyncio.run(mailbox.read_answer(correlation_id))
    assert answer is not None
    assert runner.text in answer
    assert answer.startswith(UNTRUSTED_OPEN)


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_peer_turn_that_suspends_is_not_answered_and_the_claim_is_not_held() -> None:
    """B asking its own human is the ordinary case, not a failure.

    Two things must be true afterwards: the row has no answer (an empty one would resume
    A's turn with something nobody said), and the worker has returned rather than sitting
    on the claim until B's human replies.
    """
    conninfo = _migrated_conninfo()
    _empty_the_queue(conninfo)
    session = _asking_session()
    turn_id = TurnId(str(uuid.uuid4()))
    question = f"may we refund the renewal? [{uuid.uuid4()}]"

    mailbox = PgAgentMailbox(conninfo)
    correlation_id = asyncio.run(
        mailbox.ask(
            _SUPPORT_POLICY,
            _BILLING,
            question,
            from_session=session,
            turn_id=turn_id,
            hop=1,
        )
    )

    runner = _RecordingRunner(outcome=_suspended(TurnId("peer-turn")))
    report = asyncio.run(answer_next_ask(mailbox, runner, target=_BILLING))

    assert report is not None
    assert report.disposition is AskDisposition.SUSPENDED
    assert asyncio.run(mailbox.read_answer(correlation_id)) is None


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_an_empty_queue_runs_no_turn_at_all() -> None:
    """No ask, no model call. A worker that ran a turn on an empty claim bills for nothing."""
    conninfo = _migrated_conninfo()
    runner = _RecordingRunner()

    report = asyncio.run(
        answer_next_ask(PgAgentMailbox(conninfo), runner, target=AgentId(f"nobody-{uuid.uuid4()}"))
    )

    assert report is None
    assert runner.seen == []


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_waiting_turn_is_woken_only_after_the_answer_is_on_the_row() -> None:
    """Record, THEN wake. In that order, and never the other way round.

    Waking first is a race with one loser and no error: the workflow resumes, redeems its
    correlation id through `read_answer`, finds nothing there yet, and carries on with an
    answer nobody gave. So the seat is called after `answer()` has returned, and the test
    proves the ordering by reading the row from inside the wake rather than after it.

    A SUSPENDED turn wakes nobody: there is no answer to deliver, and A must keep waiting.
    """
    conninfo = _migrated_conninfo()
    _empty_the_queue(conninfo)
    session = _asking_session()
    asking_turn = TurnId(str(uuid.uuid4()))

    mailbox = PgAgentMailbox(conninfo)
    correlation_id = asyncio.run(
        mailbox.ask(
            _SUPPORT_POLICY,
            _BILLING,
            f"is the renewal refundable? [{uuid.uuid4()}]",
            from_session=session,
            turn_id=asking_turn,
            hop=1,
        )
    )

    woken: list[tuple[TurnId, str, str | None]] = []

    async def _wake(turn_id: TurnId, correlation: str) -> None:
        woken.append((turn_id, correlation, await mailbox.read_answer(correlation)))

    runner = _RecordingRunner()
    asyncio.run(answer_next_ask(mailbox, runner, target=_BILLING, wake=_wake))

    assert len(woken) == 1
    woken_turn, woken_correlation, answer_at_wake_time = woken[0]
    # The ASKING turn's id, which is the only thing that can find the suspended workflow.
    assert woken_turn == asking_turn
    assert woken_correlation == correlation_id
    # Already on the row when the wake fired - the ordering, asserted rather than assumed.
    assert answer_at_wake_time is not None
    assert runner.text in answer_at_wake_time


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_suspended_peer_turn_wakes_nobody() -> None:
    """There is no answer to deliver, so A stays suspended rather than resuming on air."""
    conninfo = _migrated_conninfo()
    _empty_the_queue(conninfo)
    session = _asking_session()

    mailbox = PgAgentMailbox(conninfo)
    asyncio.run(
        mailbox.ask(
            _SUPPORT_POLICY,
            _BILLING,
            f"freeze the account? [{uuid.uuid4()}]",
            from_session=session,
            turn_id=TurnId(str(uuid.uuid4())),
            hop=1,
        )
    )

    woken: list[str] = []

    async def _wake(_turn_id: TurnId, correlation: str) -> None:
        woken.append(correlation)

    runner = _RecordingRunner(outcome=_suspended(TurnId("peer-turn")))
    report = asyncio.run(answer_next_ask(mailbox, runner, target=_BILLING, wake=_wake))

    assert report is not None
    assert report.disposition is AskDisposition.SUSPENDED
    assert woken == []


@dataclass
class _ScriptedQueue:
    """A queue with one ask for one target, recording the order it was polled in."""

    ask: PeerAsk
    polled: list[AgentId] = field(default_factory=list)
    answered: list[tuple[str, str]] = field(default_factory=list)

    async def claim_next(self, target: AgentId) -> PeerAsk | None:
        self.polled.append(target)
        if target == self.ask.target and self.ask.correlation_id not in {
            correlation_id for correlation_id, _ in self.answered
        }:
            return self.ask
        return None

    async def answer(self, correlation_id: str, answer: str) -> None:
        self.answered.append((correlation_id, answer))


def test_the_worker_polls_its_targets_in_a_deterministic_order() -> None:
    """Sorted, never set order (CLAUDE.md non-negotiable #7's reasoning).

    Nothing here is a DBOS workflow body, but the same argument applies to a worker that
    is restarted: two processes draining one queue in iteration order make which ask gets
    served depend on dict insertion, and a starving target is invisible.
    """
    ask = PeerAsk(
        correlation_id="c-1",
        target=_BILLING,
        question="what was charged?",
        from_session=_asking_session(),
        turn_id=TurnId("t-1"),
        hop=1,
    )
    queue = _ScriptedQueue(ask=ask)
    runner = _RecordingRunner()
    lines: list[str] = []
    passes = iter((True, True, False))

    async def _no_sleep(_seconds: float) -> None:
        return None

    targets = (_SUPPORT, _BILLING, AgentId("delivery_optimizer"))
    asyncio.run(
        run_peer_worker(
            queue,
            runner,
            targets=targets,
            sleep=_no_sleep,
            keep_going=lambda: next(passes, False),
            write_line=lines.append,
        )
    )

    assert queue.polled[: len(targets)] == sorted(targets)
    assert [correlation_id for correlation_id, _ in queue.answered] == ["c-1"]
    # One line per handled ask, naming the ask and what became of it - a worker whose only
    # output is silence is indistinguishable from one that is not draining anything.
    assert len(lines) == 1
    assert "c-1" in lines[0]
    assert AskDisposition.ANSWERED.value in lines[0]
