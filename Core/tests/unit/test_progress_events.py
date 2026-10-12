"""C21 (D52): a turn publishes `started`, `tool_called`, `finished` and `failed`.

Subjects: domain/events.py, ports/event_publisher.py, `turn_workflow._step_progress` (called from
`_drive_turn`, so root turns and peer turns alike) and the audit decorator that makes
`tool_called` (adapters/driven/events_pg/audit_events.py). The Postgres adapter is
`tests/integration/test_turn_events.py`.

What is defended:
  * the four facts, in order, each with the owner chain of its turn and the timing;
  * a publisher that fails (or is absent) never fails the turn;
  * ids are deterministic, so a replayed step publishes the same event and the store keeps one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from agent_core.adapters.driven.events_pg.audit_events import EventingAuditSink
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.events import EventKind, EventOwner, ProgressEvent, progress_event_id
from agent_core.domain.policy import Effect, PolicyDecision
from agent_core.domain.turn import (
    BackgroundReturn,
    CallerIdentity,
    PeerOwner,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    UserInput,
)
from tests.fakes import fanout
from tests.fakes import ports as fakes


class Recorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[ProgressEvent] = []
        self.fail = fail

    async def publish(self, event: ProgressEvent) -> None:
        if self.fail:
            raise RuntimeError("the event store is down")
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [e.kind.value for e in self.events]


def _bind(recorder: Recorder | None) -> None:
    deps = turn_workflow._dependencies  # noqa: SLF001
    assert deps is not None
    turn_workflow._dependencies = dataclasses.replace(deps, events=recorder)  # noqa: SLF001


def _finished(_turn_id: TurnId, _request: TurnRequest) -> Any:
    async def start(tid: TurnId, req: TurnRequest) -> TurnOutcome:
        return TurnOutcome(turn_id=tid, result=TurnResult(text="done"))

    return start


# --- ids ---


def test_an_event_id_is_deterministic_and_separates_kinds_and_turns() -> None:
    a = progress_event_id("t1", EventKind.STARTED)

    assert a == progress_event_id("t1", EventKind.STARTED)
    assert a != progress_event_id("t1", EventKind.FINISHED)
    assert a != progress_event_id("t2", EventKind.STARTED)
    assert progress_event_id("t1", EventKind.TOOL_CALLED, "1") != progress_event_id(
        "t1", EventKind.TOOL_CALLED, "2"
    )


# --- a root turn ---


def test_a_turn_publishes_started_then_finished_with_its_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fanout.install(monkeypatch, (), [], start_step=_finished(TurnId("turn-1"), fanout.request()))
    recorder = Recorder()
    _bind(recorder)

    asyncio.run(fanout.body()(fanout.request()))

    assert recorder.kinds == ["started", "finished"]
    started, finished = recorder.events
    assert (started.turn_id, started.agent, started.tenant_id) == ("turn-1", fanout.ASKER, "t")
    assert started.owner is None and started.root_turn_id == "turn-1"
    assert started.elapsed_ms is None and finished.elapsed_ms is not None
    assert finished.elapsed_ms >= 0 and finished.at >= started.at
    assert started.event_id == progress_event_id("turn-1", EventKind.STARTED)


def test_a_turn_that_raises_publishes_failed_with_the_error_class_and_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        raise ValueError("boom: secret detail")

    fanout.install(monkeypatch, (), [], start_step=explode)
    recorder = Recorder()
    _bind(recorder)

    with pytest.raises(ValueError):
        asyncio.run(fanout.body()(fanout.request()))

    assert recorder.kinds == ["started", "failed"]
    assert recorder.events[1].detail == "ValueError", "the class, never the message"
    assert "secret" not in repr(recorder.events)


@pytest.mark.parametrize("publisher", [None, "failing"])
def test_a_missing_or_failing_publisher_never_breaks_the_turn(
    monkeypatch: pytest.MonkeyPatch, publisher: str | None
) -> None:
    fanout.install(monkeypatch, (), [], start_step=_finished(TurnId("turn-1"), fanout.request()))
    _bind(None if publisher is None else Recorder(fail=True))

    outcome = asyncio.run(fanout.body()(fanout.request()))

    assert outcome.result is not None and outcome.result.text == "done"


# --- a peer's turn ---

OWNER_SESSION = SessionRef(session_id=SessionId("case-7"), tenant_id=TenantId("t"))
CALLER = CallerIdentity(subject_id="person-1", channel="http", tenant_id=TenantId("t"))


def _peer_job(*, background: bool = False) -> turn_workflow.PeerTurnJob:
    request = TurnRequest(
        session=SessionRef(session_id=SessionId("peer~case-7~x"), tenant_id=TenantId("t")),
        caller=CallerIdentity(
            subject_id="peer-agent",
            channel="peer",
            tenant_id=TenantId("t"),
            roles=frozenset({"peer"}),
        ),
        profile_id=fanout.PEER,
        input=UserInput(text="how much?"),
        hop=1,
        owner=PeerOwner(
            root_turn_id="root-turn",
            asker_turn_id="asker-turn",
            asker=fanout.ASKER,
            call="corr-9",
            background=(
                BackgroundReturn(session=OWNER_SESSION, profile_id=fanout.ASKER, caller=CALLER)
                if background
                else None
            ),
        ),
    )
    return turn_workflow.PeerTurnJob(
        request=request,
        correlation_id="corr-9",
        asking_turn_id=TurnId("asker-turn"),
        answer_token="tok",
    )


def test_a_peer_run_publishes_its_events_with_the_owner_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fanout.install(monkeypatch, (), [], start_step=_finished(TurnId("turn-1"), fanout.request()))
    recorder = Recorder()
    _bind(recorder)

    async def signal(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(turn_workflow, "signal_peer_answer", signal)

    asyncio.run(inspect.unwrap(turn_workflow.run_peer_turn_workflow)(_peer_job()))

    assert recorder.kinds == ["started", "finished"]
    for event in recorder.events:
        assert event.owner == EventOwner("root-turn", "asker-turn", fanout.ASKER, "corr-9")
        assert event.root_turn_id == "root-turn" and event.agent == fanout.PEER


# --- tool_called, from the audit trail ---


_Audit = fakes.FakeAuditSink


def test_a_recorded_tool_call_becomes_a_tool_called_event_after_the_audit_row() -> None:
    inner = _Audit()
    recorder = Recorder()
    sink = EventingAuditSink(inner, recorder, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    decision = PolicyDecision(effect=Effect.ALLOW, reason="ok", rule_id="r")

    asyncio.run(
        sink.record_tool_call(
            TurnId("callee-1"), CALLER, "lookup_balance", {"id": "SECRET"}, decision
        )
    )

    assert len(inner.calls) == 1, "the audit row comes first and is the system of record"
    (event,) = recorder.events
    assert (event.kind, event.turn_id, event.tool, event.detail) == (
        EventKind.TOOL_CALLED,
        "callee-1",
        "lookup_balance",
        "allow",
    )
    assert event.tenant_id == "t" and "SECRET" not in repr(event), "no arguments in an event"


def test_a_failing_publisher_does_not_fail_the_audited_tool_call() -> None:
    inner = _Audit()
    sink = EventingAuditSink(inner, Recorder(fail=True))
    decision = PolicyDecision(effect=Effect.ALLOW, reason="ok", rule_id="r")

    asyncio.run(sink.record_tool_call(TurnId("t"), CALLER, "tool", {}, decision))

    assert len(inner.calls) == 1


def test_the_other_audit_verbs_pass_straight_through() -> None:
    inner = _Audit()
    sink = EventingAuditSink(inner, Recorder())

    asyncio.run(sink.record_turn_end(TurnId("t"), fakes_usage(), Decimal("0.01")))

    assert [c.kind for c in inner.calls] == ["turn_end"]


def fakes_usage() -> Any:
    from agent_core.domain.turn import Usage

    return Usage()


def test_the_progress_steps_moved_the_pinned_application_version_to_ten() -> None:
    """D52 (C21): every turn now records a `started` and a `finished`/`failed` step around the
    turn, so work in flight under `.9` would replay into a different shape and is stranded on
    purpose (drain first, OPERATIONS.md). One bump for the C21-C23 batch."""
    from agent_core import composition

    assert int(composition.PINNED_DBOS_APPLICATION_VERSION.rsplit(".", 1)[1]) >= 10
