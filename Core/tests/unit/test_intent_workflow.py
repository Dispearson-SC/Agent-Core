"""An intent plan inside the durable turn (D97): recorded steps, the `planned` event, fallback.

Subject: `_run_turn`'s intent branch in adapters/driving/workflow/turn_workflow.py
(`_step_intent_prepare`, `_step_intent_tool`, `_step_intent_reply`, `_step_intent_fallback`), the
`planned` progress event and the feed's fold of it (domain/events.py).

THE RULES UNDER TEST. When the route step hands an intent on and an executor is bound, the turn
runs the plan as recorded steps (prepare, one step per call, the reply) instead of `_step_start`,
publishes `planned` (`plan`), and is delivered like any finished turn. Any failure (prepare, a
call, the reply) audits the fallback, publishes `planned` (`fallback:<reason>`) and runs the
normal path, `_step_start`, from scratch with the same request. A turn without an intent, or one
recorded by a build that had no intents, runs exactly the steps it ran before.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.application.route_turn import RouteTurn
from agent_core.application.run_intent import RunIntent
from agent_core.domain.events import (
    EventKind,
    ProgressEvent,
    StoredEvent,
    fold_snapshot,
    progress_event_id,
)
from agent_core.domain.intents import (
    PLAN_REPLY_FAILED,
    PLAN_TOOL_ERROR,
    IntentPlan,
    IntentRecord,
    PlannedCall,
    PlanStep,
    PlanToolResult,
)
from agent_core.domain.profile import AgentProfile
from agent_core.domain.routing import RouteDecision, RouteKind, RouteRecord, RouteRequest
from agent_core.domain.turn import (
    SessionRef,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
)
from tests.fakes import fanout
from tests.fakes.delegation import delegation
from tests.fakes.ports import FakeAuditSink, FakeConversationStore

OWNER = "billing"
# S8. The plan runs as its owner's peer turn would, on behalf of the fanout person `u`.
DELEGATED = {"actor": "peer-agent", "on_behalf_of": "u"}


class Recorder:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    async def publish(self, event: ProgressEvent) -> None:
        self.events.append(event)


class Router:
    def __init__(self, decision: RouteDecision) -> None:
        self.decision = decision

    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        return self.decision


class Routes:
    def __init__(self) -> None:
        self.intents: list[IntentRecord] = []

    async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
        return None

    async def record_intent(self, turn_id: TurnId, tenant_id: str, record: IntentRecord) -> None:
        self.intents.append(record)


class Context:
    def update_from_response(self, session: SessionRef, usage: Any) -> None:
        return None


class Runner:
    def __init__(self, *, failing: str | None = None, compose_error: bool = False) -> None:
        self.failing = failing
        self.compose_error = compose_error
        self.calls: list[str] = []

    async def call_tool(
        self, turn_id: TurnId, request: TurnRequest, profile: AgentProfile, call: PlannedCall
    ) -> PlanToolResult:
        self.calls.append(call.tool)
        if call.tool == self.failing:
            return PlanToolResult(call.step_id, call.tool, failure=PLAN_TOOL_ERROR)
        return PlanToolResult(call.step_id, call.tool, content=f"{call.tool}=1")

    async def compose_reply(
        self, turn_id: TurnId, request: TurnRequest, profile: AgentProfile, results: Any
    ) -> TurnOutcome:
        if self.compose_error:
            raise RuntimeError("provider down")
        return TurnOutcome(turn_id=turn_id, messages=("m",), result=TurnResult(text="composed"))

    def template_reply(self, turn_id: TurnId, text: str) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=text))


def _profiles() -> dict[str, AgentProfile]:
    asker = AgentProfile.from_mapping(
        {
            "id": fanout.ASKER,
            "persona": "p",
            "model": "m",
            "routing": {"router": "r", "targets": [OWNER], "intents": True},
        }
    )
    return {fanout.ASKER: asker, OWNER: AgentProfile(id=OWNER, persona="b", model="m")}


PLAN = IntentPlan(
    intent="balance",
    profile_id=OWNER,
    steps=(PlanStep(id="a", tool="read_a"), PlanStep(id="b", tool="read_b")),
)


class World:
    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, decision: RouteDecision, runner: Runner
    ) -> None:
        self.started: list[TurnRequest] = []
        self.delivered: list[TurnResult] = []
        self.steps: list[str] = []
        self.runner = runner
        self.routes = Routes()
        self.recorder = Recorder()

        async def start(turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
            self.started.append(request)
            return TurnOutcome(turn_id=turn_id, result=TurnResult(text="model path"))

        fanout.install(monkeypatch, (), [], start_step=start)

        async def deliver(request: TurnRequest, result: TurnResult) -> None:
            self.delivered.append(result)

        monkeypatch.setattr(turn_workflow, "_step_deliver", deliver)
        for name in (
            "_step_intent_prepare",
            "_step_intent_tool",
            "_step_intent_reply",
            "_step_intent_fallback",
        ):
            monkeypatch.setattr(turn_workflow, name, self._recording(name))
        profiles = _profiles()
        route_turn = RouteTurn(
            routers=Router(decision),
            audit=self.routes,
            profiles=profiles,
            plans={"balance": PLAN},
            **delegation(),
        )
        run_intent = RunIntent(
            runner=runner,
            store=FakeConversationStore(),
            audit=FakeAuditSink(),
            routes=self.routes,
            context=Context(),  # type: ignore[arg-type]
            profiles=profiles,
            plans={"balance": PLAN},
        )
        deps = turn_workflow._dependencies  # noqa: SLF001
        assert deps is not None
        monkeypatch.setattr(
            turn_workflow,
            "_dependencies",
            dataclasses.replace(
                deps, events=self.recorder, route_turn=route_turn, run_intent=run_intent
            ),
        )

    def _recording(self, name: str) -> Any:
        real = inspect.unwrap(getattr(turn_workflow, name))

        async def step(*args: Any) -> Any:
            self.steps.append(name)
            return await real(*args)

        return step

    def run(self) -> TurnOutcome:
        outcome: TurnOutcome = asyncio.run(fanout.body()(fanout.request()))
        return outcome

    def kinds(self) -> list[str]:
        return [e.kind.value for e in self.recorder.events]


INTENT = RouteDecision(kind=RouteKind.INTENT, intent="balance", label="balance")


def test_an_intent_turn_runs_its_plan_as_recorded_steps_and_never_the_model_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch, INTENT, Runner())

    outcome = world.run()

    assert outcome.result is not None and outcome.result.text == "composed"
    assert world.started == []
    assert world.runner.calls == ["read_a", "read_b"]
    assert world.steps == [
        "_step_intent_prepare",
        "_step_intent_tool",
        "_step_intent_tool",
        "_step_intent_reply",
    ]
    assert [r.text for r in world.delivered] == ["composed"]
    assert world.kinds() == ["routed", "started", "planned", "finished"]
    planned = world.recorder.events[2]
    assert (planned.agent, planned.detail) == (OWNER, "plan")
    assert planned.event_id == progress_event_id("turn-1", EventKind.PLANNED)
    assert world.routes.intents == [
        IntentRecord("balance", OWNER, True, 2, "compose", None, **DELEGATED)
    ]


def test_a_failed_call_stops_the_plan_and_runs_the_normal_path_from_scratch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch, INTENT, Runner(failing="read_a"))

    outcome = world.run()

    assert outcome.result is not None and outcome.result.text == "model path"
    assert world.runner.calls == ["read_a"], "the plan stops at its first failed call"
    assert [r.profile_id for r in world.started] == [OWNER]
    assert world.steps == ["_step_intent_prepare", "_step_intent_tool", "_step_intent_fallback"]
    assert world.kinds() == ["routed", "started", "planned", "finished"]
    assert world.recorder.events[2].detail == f"fallback:{PLAN_TOOL_ERROR}"
    assert world.routes.intents == [
        IntentRecord("balance", OWNER, False, 1, "compose", PLAN_TOOL_ERROR, **DELEGATED)
    ]


def test_a_reply_that_fails_falls_back_after_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, INTENT, Runner(compose_error=True))

    world.run()

    assert [r.profile_id for r in world.started] == [OWNER]
    assert world.steps[-2:] == ["_step_intent_reply", "_step_intent_fallback"]
    assert world.routes.intents[-1].fallback_reason == PLAN_REPLY_FAILED


def test_a_turn_without_an_intent_runs_exactly_the_steps_it_ran_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch, RouteDecision(kind=RouteKind.ROUTE, profile_id=OWNER), Runner())

    world.run()

    assert [r.profile_id for r in world.started] == [OWNER]
    assert world.steps == []
    assert world.kinds() == ["routed", "started", "finished"]


def test_a_route_recorded_by_a_build_without_intents_runs_the_normal_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch, INTENT, Runner())

    class OldRoutedTurn:  # what pickle hands back for a `.12` record: no `intent` slot
        __slots__ = ("record", "request")

        def __init__(self, request: TurnRequest) -> None:
            self.request = request
            self.record = None

    async def replayed(request: TurnRequest) -> Any:
        return OldRoutedTurn(dataclasses.replace(request, profile_id=OWNER))

    monkeypatch.setattr(turn_workflow, "_step_route", replayed)

    world.run()

    assert [r.profile_id for r in world.started] == [OWNER]
    assert world.steps == []


def _recovered_after_routing_was_turned_off(
    monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    """The route step's recorded result (an intent) is read back, but the redeployed process has
    no executor bound: the operator turned routing off between the run and its recovery."""
    from agent_core.application.route_turn import RoutedTurn
    from agent_core.domain.intents import IntentCall

    async def replayed(request: TurnRequest) -> Any:
        routed = dataclasses.replace(request, profile_id=OWNER)
        return RoutedTurn(routed, None, IntentCall("balance"))

    monkeypatch.setattr(turn_workflow, "_step_route", replayed)
    deps = turn_workflow._dependencies  # noqa: SLF001
    assert deps is not None
    monkeypatch.setattr(turn_workflow, "_dependencies", dataclasses.replace(deps, run_intent=None))


def test_a_recovered_intent_turn_keeps_its_step_shape_after_routing_is_turned_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 36 (replay): which steps a turn runs is a function of recorded results only. A turn
    whose recorded route handed an intent on runs the prepare step even when no executor is bound
    any more (a recovered run reads its recorded result back; a fresh one records `not_wired`), so
    a config flip between the run and its recovery never changes the operation log
    (DBOSUnexpectedStepError, the P1 lesson); it then falls back to the normal path."""
    from agent_core.domain.intents import PLAN_NOT_WIRED

    world = World(monkeypatch, INTENT, Runner())
    _recovered_after_routing_was_turned_off(monkeypatch, world)

    outcome = world.run()

    assert outcome.result is not None and outcome.result.text == "model path"
    assert world.steps == ["_step_intent_prepare", "_step_intent_fallback"]
    assert [r.profile_id for r in world.started] == [OWNER]
    assert world.recorder.events[-2].detail == f"fallback:{PLAN_NOT_WIRED}"


def test_a_turn_recovered_mid_plan_after_routing_is_turned_off_falls_back_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prepare step was recorded with its calls before the crash; the call steps that had not
    run yet find no executor and fail as calls (`not_wired`), never as an unwired turn."""
    from agent_core.application.run_intent import PreparedPlan
    from agent_core.domain.intents import PLAN_NOT_WIRED

    world = World(monkeypatch, INTENT, Runner())
    _recovered_after_routing_was_turned_off(monkeypatch, world)

    async def recorded(turn_id: TurnId, request: TurnRequest, call: Any) -> PreparedPlan:
        world.steps.append("_step_intent_prepare")
        return PreparedPlan(calls=(PlannedCall("a", "read_a", {}), PlannedCall("b", "read_b", {})))

    monkeypatch.setattr(turn_workflow, "_step_intent_prepare", recorded)

    outcome = world.run()

    assert outcome.result is not None and outcome.result.text == "model path"
    assert world.steps == [
        "_step_intent_prepare",
        "_step_intent_tool",
        "_step_intent_fallback",
    ]
    assert world.recorder.events[-2].detail == f"fallback:{PLAN_NOT_WIRED}"
    assert world.runner.calls == []


def test_the_intent_steps_are_durable_steps() -> None:
    for name in (
        "_step_intent_prepare",
        "_step_intent_tool",
        "_step_intent_reply",
        "_step_intent_fallback",
    ):
        step = getattr(turn_workflow, name)
        assert inspect.unwrap(step) is not step, name


def test_a_planned_event_records_the_plan_on_its_turn_in_the_feed() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)

    def stored(seq: int, kind: EventKind, detail: str | None = None) -> StoredEvent:
        return StoredEvent(
            seq=seq,
            event=ProgressEvent(
                event_id=f"e{seq}", kind=kind, tenant_id="t", session_id="s",
                turn_id="turn-1", agent=OWNER, at=at, detail=detail,
            ),
        )

    snapshot = fold_snapshot(
        "turn-1",
        (
            stored(1, EventKind.ROUTED, "routed"),
            stored(2, EventKind.STARTED),
            stored(3, EventKind.PLANNED, "fallback:tool_error"),
        ),
    )
    (turn,) = snapshot.turns
    assert turn.plan == "fallback:tool_error"
    assert turn.state.value == "running" and turn.tools_called == 0
