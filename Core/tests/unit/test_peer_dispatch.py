"""The peer worker hands a claimed ask to a DURABLE starter instead of running the turn (C1).

Subject: adapters/driving/peers/worker.py (`dispatch_next_ask`, `PeerTurnStarter`,
`run_peer_worker`).

The worker used to run the callee turn itself and answer on return. A turn that suspended (its
own human, a third agent) answered nobody; a worker that died after the claim lost the row. Now
the worker claims, builds the request AS THE TARGET AGENT, and starts a workflow that owns the
rest. It never answers a started ask itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from agent_core.adapters.driven.peers.mailbox import PeerAsk
from agent_core.adapters.driving.peers.worker import (
    PEER_CHANNEL,
    PEER_ROLE,
    PEER_SUBJECT_ID,
    AskDisposition,
    PeerTurnStarter,
    answer_next_ask,
    dispatch_next_ask,
    run_peer_worker,
)
from agent_core.domain.peers import (
    PEER_ASK_FAILED_ANSWER,
    AgentId,
    PeerAnswerRefusedError,
    peer_session_key,
)
from agent_core.domain.turn import (
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
)

ORIGIN = SessionRef(session_id=SessionId("case-77"), tenant_id=TenantId("S030"))


def _ask(corr: str = "c1", hop: int = 1, token: str | None = "tok-c1") -> PeerAsk:
    return PeerAsk(
        correlation_id=corr,
        target=AgentId("specialist"),
        question="plan?",
        from_session=ORIGIN,
        turn_id=TurnId("asker-turn"),
        hop=hop,
        asker=AgentId("coordinator"),
        answer_token=token,
    )


def _redelivered(times: int, *, failures: int = 0) -> PeerAsk:
    base = _ask()
    return PeerAsk(
        correlation_id=base.correlation_id,
        target=base.target,
        question=base.question,
        from_session=base.from_session,
        turn_id=base.turn_id,
        hop=base.hop,
        asker=base.asker,
        answer_token=base.answer_token,
        redeliveries=times,
        failures=failures,
    )


@dataclass
class _Queue:
    asks: list[PeerAsk]
    answered: list[tuple[str, str]] = field(default_factory=list)
    tokens: list[str | None] = field(default_factory=list)

    async def claim_next(self, target: AgentId) -> PeerAsk | None:
        return self.asks.pop(0) if self.asks else None

    async def answer(
        self,
        correlation_id: str,
        answer: str,
        *,
        answer_token: str | None = None,
        failed: bool = False,
    ) -> None:
        self.answered.append((correlation_id, answer))
        self.tokens.append(answer_token)


@dataclass
class _Starter:
    started: list[tuple[str, TurnId, TurnRequest]] = field(default_factory=list)
    tokens: list[str | None] = field(default_factory=list)
    error: BaseException | None = None
    # C1d: the enqueue COMMITTED before the call failed (a lost ack, a shutdown).
    commits_before_failing: bool = False
    exists_error: Exception | None = None
    exists_calls: list[str] = field(default_factory=list)
    retired: list[tuple[str, int]] = field(default_factory=list)
    generations: list[int] = field(default_factory=list)
    failed_generations: set[tuple[str, int]] = field(default_factory=set)

    async def failed(self, correlation_id: str, generation: int = 0) -> bool:
        return (correlation_id, generation) in self.failed_generations

    async def exists(self, correlation_id: str, generation: int = 0) -> bool:
        self.exists_calls.append(correlation_id)
        if self.exists_error is not None:
            raise self.exists_error
        return any(started[0] == correlation_id for started in self.started)

    async def retire(self, correlation_id: str, generation: int = 0) -> None:
        self.retired.append((correlation_id, generation))

    async def start(
        self,
        correlation_id: str,
        asking_turn_id: TurnId,
        request: TurnRequest,
        *,
        answer_token: str | None = None,
        generation: int = 0,
    ) -> None:
        self.generations.append(generation)
        if self.error is not None:
            if self.commits_before_failing:
                self.started.append((correlation_id, asking_turn_id, request))
            raise self.error
        self.started.append((correlation_id, asking_turn_id, request))
        self.tokens.append(answer_token)


def test_a_claimed_ask_starts_the_callee_turn_as_the_target_and_is_not_answered_by_the_worker() -> (
    None
):
    queue, starter, woken = _Queue([_ask()]), _Starter(), []

    async def wake(turn_id: TurnId, correlation_id: str) -> None:
        woken.append((turn_id, correlation_id))

    report = asyncio.run(dispatch_next_ask(queue, starter, target=AgentId("specialist"), wake=wake))

    assert report is not None and report.disposition is AskDisposition.STARTED
    ((corr, asking_turn, request),) = starter.started
    assert (corr, asking_turn) == ("c1", TurnId("asker-turn"))
    assert request.profile_id == "specialist", "the TARGET's profile, never the asker's"
    assert request.caller.subject_id == PEER_SUBJECT_ID
    assert request.caller.channel == PEER_CHANNEL
    assert request.caller.roles == frozenset({PEER_ROLE})
    assert request.hop == 1, "the hop count travels onto the callee's turn"
    assert request.input.text == "plan?"
    assert request.session.session_id == f"peer~case-77~{peer_session_key('c1')}"
    assert request.session.tenant_id == "S030"
    assert queue.answered == [] and woken == [], "the workflow answers, not the worker"


def test_an_empty_queue_starts_nothing() -> None:
    starter = _Starter()

    report = asyncio.run(dispatch_next_ask(_Queue([]), starter, target=AgentId("specialist")))

    assert report is None and starter.started == []


def test_claiming_the_same_row_again_starts_the_same_workflow_inputs() -> None:
    """The worker may die after the claim and a retry may claim again: same ids, same session."""
    starter = _Starter()

    asyncio.run(dispatch_next_ask(_Queue([_ask()]), starter, target=AgentId("specialist")))
    asyncio.run(dispatch_next_ask(_Queue([_ask()]), starter, target=AgentId("specialist")))

    first, second = starter.started
    assert first == second


def test_a_redelivered_ask_starts_the_generation_its_row_names() -> None:
    """C1g. The lease counts redeliveries on the row; the claim carries the count and the worker
    starts the workflow of that generation, so a rerun does not collide with a stranded one."""
    redelivered = _redelivered(2)
    starter = _Starter()

    asyncio.run(dispatch_next_ask(_Queue([redelivered]), starter, target=AgentId("specialist")))

    assert starter.generations == [2]


def test_a_failed_start_asks_about_the_generation_it_was_starting() -> None:
    starter = _Starter(error=RuntimeError("down"), exists_calls=[])
    seen: list[tuple[str, int]] = []

    async def exists(correlation_id: str, generation: int = 0) -> bool:
        seen.append((correlation_id, generation))
        return True  # the enqueue committed: the ask must stay open

    starter.exists = exists  # type: ignore[method-assign]
    queue = _Queue([_redelivered(3)])

    with pytest.raises(RuntimeError, match="down"):
        asyncio.run(dispatch_next_ask(queue, starter, target=AgentId("specialist")))

    assert seen == [("c1", 3)]
    assert queue.answered == []


def test_a_start_that_fails_closes_the_ask_with_fixed_text_and_raises() -> None:
    queue = _Queue([_ask()])
    starter = _Starter(error=RuntimeError("queue unavailable: secret dsn"))
    woken: list[tuple[TurnId, str]] = []

    async def wake(turn_id: TurnId, correlation_id: str) -> None:
        woken.append((turn_id, correlation_id))

    with pytest.raises(RuntimeError, match="queue unavailable"):
        asyncio.run(dispatch_next_ask(queue, starter, target=AgentId("specialist"), wake=wake))

    assert queue.answered == [("c1", PEER_ASK_FAILED_ANSWER)]
    assert woken == [(TurnId("asker-turn"), "c1")]


def test_the_worker_loop_uses_the_starter_when_it_is_given_one() -> None:
    queue, starter, lines = _Queue([_ask("a"), _ask("b")]), _Starter(), []
    passes = iter([True, True, False])

    asyncio.run(
        run_peer_worker(
            queue,
            starter,
            targets=[AgentId("specialist")],
            keep_going=lambda: next(passes),
            write_line=lines.append,
        )
    )

    assert [corr for corr, _, _ in starter.started] == ["a", "b"]
    assert lines == ["[peer] specialist a started", "[peer] specialist b started"]


def test_a_starter_is_recognised_by_shape() -> None:
    assert isinstance(_Starter(), PeerTurnStarter)


def test_the_answer_token_travels_from_the_claim_to_the_workflow_start() -> None:
    """C1c. The secret is on the claimed row and goes to the durable starter, which hands it
    to the workflow that is the only legitimate answerer."""
    starter = _Starter()

    asyncio.run(dispatch_next_ask(_Queue([_ask()]), starter, target=AgentId("specialist")))

    assert starter.tokens == ["tok-c1"]


def test_closing_a_failed_start_presents_the_token_too() -> None:
    queue = _Queue([_ask()])
    starter = _Starter(error=RuntimeError("down"))

    with pytest.raises(RuntimeError):
        asyncio.run(dispatch_next_ask(queue, starter, target=AgentId("specialist")))

    assert queue.tokens == ["tok-c1"]


def _dispatch_failing(
    starter: _Starter, queue: _Queue, error_type: type[BaseException]
) -> list[tuple[TurnId, str]]:
    woken: list[tuple[TurnId, str]] = []

    async def wake(turn_id: TurnId, correlation_id: str) -> None:
        woken.append((turn_id, correlation_id))

    with pytest.raises(error_type):
        asyncio.run(dispatch_next_ask(queue, starter, target=AgentId("specialist"), wake=wake))
    return woken


def test_a_start_that_committed_before_failing_does_not_close_the_ask() -> None:
    """C1d. A lost ack after the enqueue committed: the workflow exists and will answer. Closing
    the ask with the failure text would hand the asker a wrong answer first."""
    queue = _Queue([_ask()])
    starter = _Starter(error=RuntimeError("ack lost"), commits_before_failing=True)

    woken = _dispatch_failing(starter, queue, RuntimeError)

    assert starter.exists_calls == ["c1"], "asked whether peer-<id> exists before deciding"
    assert queue.answered == [] and woken == []


def test_a_cancelled_start_whose_workflow_exists_does_not_close_the_ask_and_reraises() -> None:
    queue = _Queue([_ask()])
    starter = _Starter(error=asyncio.CancelledError(), commits_before_failing=True)

    woken = _dispatch_failing(starter, queue, asyncio.CancelledError)

    assert queue.answered == [] and woken == []


def test_a_cancelled_start_that_definitely_enqueued_nothing_closes_the_ask() -> None:
    queue = _Queue([_ask()])
    starter = _Starter(error=asyncio.CancelledError())

    woken = _dispatch_failing(starter, queue, asyncio.CancelledError)

    assert queue.answered == [("c1", PEER_ASK_FAILED_ANSWER)]
    assert woken == [(TurnId("asker-turn"), "c1")]


def test_when_existence_cannot_be_determined_the_ask_is_left_open_for_redelivery() -> None:
    """Unknown is not 'does not exist': closing could hide a live workflow's answer. The row
    stays delivered and the C3 lease reclaims it."""
    queue = _Queue([_ask()])
    starter = _Starter(error=RuntimeError("down"), exists_error=ConnectionError("db down"))

    woken = _dispatch_failing(starter, queue, RuntimeError)

    assert queue.answered == [] and woken == []


# --- C1k (R4-002): a callee that keeps dying is not rerun for ever --------------------------------


def test_an_ask_past_its_redelivery_cap_is_closed_with_the_failure_answer_and_the_asker_woken() -> (
    None
):
    """A callee that deterministically ends in ERROR is 'absent' to the lease, so it is requeued
    every lease for ever (the poison budget restarts per generation). Past the cap the claim
    closes the ask instead of starting another generation."""
    queue, starter, woken = _Queue([_redelivered(4, failures=4)]), _Starter(), []

    async def wake(turn_id: TurnId, correlation_id: str) -> None:
        woken.append((turn_id, correlation_id))

    report = asyncio.run(
        dispatch_next_ask(
            queue, starter, target=AgentId("specialist"), wake=wake, max_redeliveries=3
        )
    )

    assert report is not None and report.disposition is AskDisposition.ABANDONED
    assert starter.started == [], "no new generation is started"
    assert queue.answered == [("c1", PEER_ASK_FAILED_ANSWER)]
    assert queue.tokens == ["tok-c1"]
    assert woken == [(TurnId("asker-turn"), "c1")]


def test_an_ask_at_its_last_allowed_redelivery_still_starts() -> None:
    queue, starter = _Queue([_redelivered(3, failures=3)]), _Starter()

    report = asyncio.run(
        dispatch_next_ask(queue, starter, target=AgentId("specialist"), max_redeliveries=3)
    )

    assert report is not None and report.disposition is AskDisposition.STARTED
    assert starter.generations == [3]


def test_requeues_that_were_not_callee_failures_never_use_up_the_cap() -> None:
    """C1l. A worker that died between claim and start, or a workflow retired by a deploy, ends a
    generation too. Only a callee FAILURE counts toward `AGENT_CORE_PEER_MAX_REDELIVERIES`."""
    queue, starter = _Queue([_redelivered(40, failures=0)]), _Starter()

    report = asyncio.run(
        dispatch_next_ask(queue, starter, target=AgentId("specialist"), max_redeliveries=3)
    )

    assert report is not None and report.disposition is AskDisposition.STARTED
    assert starter.generations == [40], "the generation is still the row's redeliveries"


def test_without_a_cap_any_generation_starts() -> None:
    queue, starter = _Queue([_redelivered(40)]), _Starter()

    asyncio.run(dispatch_next_ask(queue, starter, target=AgentId("specialist")))

    assert starter.generations == [40]


def test_the_worker_loop_applies_its_redelivery_cap() -> None:
    queue, starter, lines = _Queue([_redelivered(9, failures=9)]), _Starter(), []
    passes = iter([True, False])

    asyncio.run(
        run_peer_worker(
            queue,
            starter,
            targets=[AgentId("specialist")],
            max_redeliveries=2,
            keep_going=lambda: next(passes),
            write_line=lines.append,
        )
    )

    assert starter.started == []
    assert queue.answered == [("c1", PEER_ASK_FAILED_ANSWER)]
    assert any("abandoned" in line for line in lines)


# --- C3: the worker loop reclaims stale deliveries ---------------------------------------------


@dataclass
class _ReclaimingQueue(_Queue):
    reclaim_calls: list[tuple[str, timedelta]] = field(default_factory=list)
    exists_seen: list[bool] = field(default_factory=list)
    failed_seen: list[bool] = field(default_factory=list)

    async def reclaim_stale(
        self,
        target: AgentId,
        *,
        lease: timedelta,
        now: datetime | None = None,
        exists: Callable[[str, int], Awaitable[bool]],
        retire: Callable[[str, int], Awaitable[None]] | None = None,
        failed: Callable[[str, int], Awaitable[bool]] | None = None,
    ) -> tuple[str, ...]:
        self.reclaim_calls.append((str(target), lease))
        self.exists_seen.append(await exists("c-stale", 2))
        if failed is not None:
            self.failed_seen.append(await failed("c-stale", 2))
        if retire is not None:
            await retire("c-stale", 2)
        return ("c-stale",)


def test_each_pass_reclaims_stale_deliveries_before_claiming_when_a_lease_is_set() -> None:
    queue, starter, lines = _ReclaimingQueue([_ask("a")]), _Starter(), []
    passes = iter([True, False])

    asyncio.run(
        run_peer_worker(
            queue,
            starter,
            targets=[AgentId("specialist")],
            lease=timedelta(minutes=5),
            keep_going=lambda: next(passes),
            write_line=lines.append,
        )
    )

    assert queue.reclaim_calls == [("specialist", timedelta(minutes=5))]
    assert starter.exists_calls == ["c-stale"], "the starter's existence check is what is asked"
    assert queue.exists_seen == [False]
    assert starter.retired == [("c-stale", 2)], "the starter's retire is what the lease calls"
    assert any("reclaimed c-stale" in line for line in lines)
    assert [corr for corr, _, _ in starter.started] == ["a"], "claiming still happens"


def test_the_lease_asks_the_starter_whether_the_callee_failed() -> None:
    """C1l. The worker hands the starter's `failed` verdict to the lease, which counts it."""
    queue, starter = _ReclaimingQueue([]), _Starter(failed_generations={("c-stale", 2)})
    passes = iter([True, False])

    asyncio.run(
        run_peer_worker(
            queue,
            starter,
            targets=[AgentId("specialist")],
            lease=timedelta(minutes=5),
            keep_going=lambda: next(passes),
            sleep=_no_sleep,
        )
    )

    assert queue.failed_seen == [True]


def test_without_a_lease_nothing_is_reclaimed() -> None:
    queue, starter = _ReclaimingQueue([]), _Starter()
    passes = iter([True, False])

    asyncio.run(
        run_peer_worker(
            queue,
            starter,
            targets=[AgentId("specialist")],
            keep_going=lambda: next(passes),
            sleep=_no_sleep,
        )
    )

    assert queue.reclaim_calls == []


def test_a_lease_needs_a_queue_that_can_reclaim() -> None:
    with pytest.raises(TypeError, match="reclaim_stale"):
        asyncio.run(
            run_peer_worker(
                _Queue([]),
                _Starter(),
                targets=[AgentId("specialist")],
                lease=timedelta(minutes=5),
                keep_going=lambda: False,
            )
        )


async def _no_sleep(seconds: float) -> None:
    return None


# --- C1h: the non-durable answer path meets a cancelled ask -------------------------------------


@dataclass
class _CancelledQueue(_Queue):
    """A queue whose row was cancelled (the asker gave up) while the callee was running."""

    async def answer(
        self,
        correlation_id: str,
        answer: str,
        *,
        answer_token: str | None = None,
        failed: bool = False,
    ) -> None:
        raise PeerAnswerRefusedError(correlation_id, "the ask was cancelled")


class _Finishes:
    async def execute(self, turn_id: TurnId, request: TurnRequest, *, hop: int) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="too late"))


def test_the_non_durable_path_survives_an_answer_for_a_cancelled_ask() -> None:
    """`answer_next_ask` records the callee's reply with `queue.answer`; a cancelled row refuses
    it. That is an expected end, not a worker crash: nothing is written, nobody is woken, and the
    report says the ask was cancelled so the loop's log line shows it."""
    woken: list[tuple[TurnId, str]] = []

    async def wake(turn_id: TurnId, correlation_id: str) -> None:
        woken.append((turn_id, correlation_id))

    report = asyncio.run(
        answer_next_ask(
            _CancelledQueue([_ask()]), _Finishes(), target=AgentId("specialist"), wake=wake
        )
    )

    assert report is not None and report.disposition is AskDisposition.REFUSED
    assert report.correlation_id == "c1"
    assert woken == [], "the asker moved on: a late reply wakes nobody"
