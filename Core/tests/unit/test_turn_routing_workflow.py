"""The turn router in the durable turn: one recorded step before the turn starts (D95).

Subject: `_step_route` and `run_turn_workflow` in adapters/driving/workflow/turn_workflow.py, the
`routed` progress event and the feed's fold of it (domain/events.py).

THE RULES UNDER TEST. With routing off (no `route_turn` bound) the turn starts with the request it
was given and publishes exactly what it did before. With it on, the turn starts as the routed
profile, a `routed` event precedes `started`, and the decision is the STEP's recorded result: a
replayed workflow reads it back and never asks the router again, even when the router would now
answer differently. The pinned DBOS application version moved because the body gained the step.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from datetime import UTC, datetime

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.application.route_turn import RoutedTurn, RouteTurn
from agent_core.domain.events import (
    EventKind,
    ProgressEvent,
    StoredEvent,
    TurnState,
    fold_snapshot,
    progress_event_id,
)
from agent_core.domain.profile import AgentProfile
from agent_core.domain.routing import RouteDecision, RouteKind, RouteRecord, RouteRequest
from agent_core.domain.turn import TurnId, TurnOutcome, TurnRequest, TurnResult
from tests.fakes import fanout

BILLING = "billing"


class Recorder:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    async def publish(self, event: ProgressEvent) -> None:
        self.events.append(event)


class Router:
    def __init__(self, decision: RouteDecision) -> None:
        self.decision = decision
        self.calls = 0

    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        self.calls += 1
        return self.decision


class Routes:
    def __init__(self) -> None:
        self.records: list[RouteRecord] = []

    async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
        self.records.append(record)


def _profiles() -> dict[str, AgentProfile]:
    asker = AgentProfile.from_mapping(
        {
            "id": fanout.ASKER,
            "persona": "p",
            "model": "m",
            "routing": {"router": "keyword", "targets": [BILLING]},
        }
    )
    return {fanout.ASKER: asker, BILLING: AgentProfile(id=BILLING, persona="b", model="m")}


def _install(
    monkeypatch: pytest.MonkeyPatch, router: Router | None
) -> tuple[list[TurnRequest], Recorder]:
    started: list[TurnRequest] = []

    async def start(turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        started.append(request)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="done"))

    fanout.install(monkeypatch, (), [], start_step=start)
    recorder = Recorder()
    route_turn = (
        None
        if router is None
        else RouteTurn(routers=router, audit=Routes(), profiles=_profiles())
    )
    deps = turn_workflow._dependencies  # noqa: SLF001
    assert deps is not None
    turn_workflow._dependencies = dataclasses.replace(  # noqa: SLF001
        deps, events=recorder, route_turn=route_turn
    )
    return started, recorder


def test_with_routing_off_the_turn_starts_as_requested_and_publishes_what_it_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, recorder = _install(monkeypatch, None)

    asyncio.run(fanout.body()(fanout.request()))

    assert started == [fanout.request()]
    assert [e.kind.value for e in recorder.events] == ["started", "finished"]


def test_a_routed_turn_starts_as_the_routed_profile_after_a_routed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = Router(RouteDecision(kind=RouteKind.ROUTE, profile_id=BILLING, label="billing"))
    started, recorder = _install(monkeypatch, router)

    asyncio.run(fanout.body()(fanout.request()))

    assert [r.profile_id for r in started] == [BILLING]
    assert [e.kind.value for e in recorder.events] == ["routed", "started", "finished"]
    routed, start = recorder.events[0], recorder.events[1]
    assert (routed.agent, routed.detail, routed.turn_id) == (BILLING, "routed", "turn-1")
    assert routed.elapsed_ms is not None and routed.owner is None
    assert routed.event_id == progress_event_id("turn-1", EventKind.ROUTED)
    assert "billing" not in (routed.detail or "").replace("routed", ""), "no label on the feed"
    assert start.agent == BILLING


def test_a_fallback_is_published_with_its_reason_and_the_turn_runs_as_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = Router(RouteDecision(kind=RouteKind.INTENT, intent="refund"))
    started, recorder = _install(monkeypatch, router)

    asyncio.run(fanout.body()(fanout.request()))

    assert [r.profile_id for r in started] == [fanout.ASKER]
    assert recorder.events[0].detail == "fallback:intent_not_supported"


def test_a_replayed_turn_reads_the_recorded_route_and_never_asks_the_router_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The router would now say pass; the operation log says the turn was routed to billing.
    router = Router(RouteDecision(kind=RouteKind.PASS))
    started, _ = _install(monkeypatch, router)
    recorded = RoutedTurn(
        dataclasses.replace(fanout.request(), profile_id=BILLING),
        RouteRecord(
            router="keyword", requested_profile=fanout.ASKER, profile_id=BILLING,
            decision=RouteKind.ROUTE, elapsed_ms=3,
        ),
    )

    async def replayed(request: TurnRequest) -> RoutedTurn:
        return recorded  # what DBOS hands back for a step that already completed

    monkeypatch.setattr(turn_workflow, "_step_route", replayed)

    asyncio.run(fanout.body()(fanout.request()))

    assert [r.profile_id for r in started] == [BILLING]
    assert router.calls == 0


def test_the_route_step_is_a_durable_step() -> None:
    step = turn_workflow._step_route  # noqa: SLF001
    assert inspect.unwrap(step) is not step, "the decision must be a recorded step result"


def test_a_routed_event_never_changes_the_state_of_a_turn_in_the_feed() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)

    def stored(seq: int, kind: EventKind) -> StoredEvent:
        return StoredEvent(
            seq=seq,
            event=ProgressEvent(
                event_id=f"e{seq}", kind=kind, tenant_id="t", session_id="s",
                turn_id="turn-1", agent=BILLING, at=at, detail="routed",
            ),
        )

    for omitted in (0, 3):
        before = fold_snapshot("turn-1", (stored(1, EventKind.ROUTED),), omitted=omitted)
        assert before.turns == ()
        running = fold_snapshot(
            "turn-1",
            (stored(1, EventKind.ROUTED), stored(2, EventKind.STARTED)),
            omitted=omitted,
        )
        assert [t.state for t in running.turns] == [TurnState.RUNNING]


def test_the_route_step_moved_the_pinned_application_version_to_twelve() -> None:
    """D95: the body gained `_step_route` before the turn, so work in flight under `.11` would
    replay into a different shape. Stranded on purpose (drain first, OPERATIONS.md)."""
    from agent_core import composition

    assert int(composition.PINNED_DBOS_APPLICATION_VERSION.rsplit(".", 1)[1]) >= 12
