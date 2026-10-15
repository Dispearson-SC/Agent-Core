"""A fast path composite inside the durable turn (Phase 5, S10): recorded steps and fallback.

Subject: `_run_turn`'s composite branch in adapters/driving/workflow/turn_workflow.py
(`_step_composite_run`, `_step_composite_reply`, `_step_composite_fallback`) and the `planned`
progress event.

THE RULES UNDER TEST. When the route step hands a composite on, the turn runs it as three recorded
steps at most (every call in one step, the one reply, or the fallback) instead of `_step_start`,
publishes `planned` (`composite`) and is delivered as the person's turn of the entry profile. Any
failure audits the fallback, publishes `planned` (`fallback:<reason>`) and runs the entry profile's
normal path from scratch. With the fast path off (or shadow) the steps are never reached and the
turn runs exactly what it ran before. A composite recovered after routing was turned off keeps its
step shape and falls back as `not_wired`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from typing import Any

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.application.route_turn import RoutedTurn, RouteTurn
from agent_core.application.run_intent import RunIntent
from agent_core.domain.events import EventKind, ProgressEvent
from agent_core.domain.fast_path import (
    Assessment,
    CompositeCall,
    CompositeMember,
    FastPathMode,
)
from agent_core.domain.intents import (
    PLAN_NOT_WIRED,
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
from agent_core.domain.turn import SessionRef, TurnId, TurnOutcome, TurnRequest, TurnResult
from tests.fakes import fanout
from tests.fakes.delegation import delegation
from tests.fakes.ports import FakeAuditSink, FakeConversationStore

SALES, STOCK = "sales", "stock"
PLANS = {
    "day_summary": IntentPlan("day_summary", SALES, (PlanStep("snap", "get_snapshot"),)),
    "order_plan": IntentPlan("order_plan", STOCK, (PlanStep("plan", "get_order_plan"),)),
}
DECISION = RouteDecision(
    kind=RouteKind.ROUTE,
    profile_id=SALES,
    label="day_summary",
    confidence=0.7,
    assessment=Assessment.of(
        {"day_summary": 0.93, "order_plan": 0.81}, action=0.01, complexity=0.1
    ),
)


class Recorder:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    async def publish(self, event: ProgressEvent) -> None:
        self.events.append(event)


class Router:
    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        return DECISION


class Routes:
    def __init__(self) -> None:
        self.records: list[RouteRecord] = []
        self.intents: list[IntentRecord] = []

    async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
        self.records.append(record)

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

    async def compose_composite(
        self, turn_id: TurnId, request: TurnRequest, profile: AgentProfile, groups: Any
    ) -> TurnOutcome:
        if self.compose_error:
            raise RuntimeError("provider down")
        return TurnOutcome(turn_id=turn_id, messages=("m",), result=TurnResult(text="composite"))

    def template_reply(self, turn_id: TurnId, text: str) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=text))


def _profiles() -> dict[str, AgentProfile]:
    asker = AgentProfile.from_mapping(
        {
            "id": fanout.ASKER,
            "persona": "p",
            "model": "m",
            "routing": {"router": "r", "targets": [SALES, STOCK], "intents": True},
        }
    )
    return {
        fanout.ASKER: asker,
        SALES: AgentProfile(id=SALES, persona="s", model="m"),
        STOCK: AgentProfile(id=STOCK, persona="k", model="m"),
    }


STEPS = ("_step_composite_run", "_step_composite_reply", "_step_composite_fallback")


class World:
    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, runner: Runner, mode: FastPathMode
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
        for name in STEPS:
            monkeypatch.setattr(turn_workflow, name, self._recording(name))
        profiles = _profiles()
        route_turn = RouteTurn(
            routers=Router(),
            audit=self.routes,
            profiles=profiles,
            plans=PLANS,
            fast_path=mode,
            **delegation(),
        )
        self.run_intent = RunIntent(
            runner=runner,  # type: ignore[arg-type]
            store=FakeConversationStore(),
            audit=FakeAuditSink(),
            routes=self.routes,
            context=Context(),  # type: ignore[arg-type]
            profiles=profiles,
            plans=PLANS,
        )
        deps = turn_workflow._dependencies  # noqa: SLF001
        assert deps is not None
        monkeypatch.setattr(
            turn_workflow,
            "_dependencies",
            dataclasses.replace(
                deps, events=self.recorder, route_turn=route_turn, run_intent=self.run_intent
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

    def planned(self) -> list[str | None]:
        return [e.detail for e in self.recorder.events if e.kind is EventKind.PLANNED]


def test_on_a_composite_turn_runs_one_step_for_every_call_and_one_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch, Runner(), FastPathMode.ON)

    outcome = world.run()

    assert outcome.result is not None and outcome.result.text == "composite"
    assert world.started == []
    assert sorted(world.runner.calls) == ["get_order_plan", "get_snapshot"]
    assert world.steps == ["_step_composite_run", "_step_composite_reply"]
    assert [r.text for r in world.delivered] == ["composite"]
    kinds = [e.kind.value for e in world.recorder.events]
    assert kinds == ["routed", "started", "planned", "finished"]
    assert world.recorder.events[0].detail == "composite"
    assert world.planned() == ["composite"]
    assert world.recorder.events[1].agent == fanout.ASKER, "the person's turn, entry profile"
    assert [r.intent for r in world.routes.intents] == ["day_summary", "order_plan"]


def test_on_a_failed_call_runs_the_entry_profiles_normal_path_from_scratch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch, Runner(failing="get_order_plan"), FastPathMode.ON)

    outcome = world.run()

    assert outcome.result is not None and outcome.result.text == "model path"
    assert [r.profile_id for r in world.started] == [fanout.ASKER]
    assert world.started[0].caller.on_behalf_of is None, "the normal path runs as the person"
    assert world.steps == ["_step_composite_run", "_step_composite_fallback"]
    assert world.planned() == [f"fallback:{PLAN_TOOL_ERROR}"]
    assert {r.fallback_reason for r in world.routes.intents} == {PLAN_TOOL_ERROR}


def test_on_a_reply_that_fails_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, Runner(compose_error=True), FastPathMode.ON)
    world.run()
    assert world.steps == list(STEPS)
    assert world.planned() == [f"fallback:{PLAN_REPLY_FAILED}"]
    assert [r.profile_id for r in world.started] == [fanout.ASKER]


@pytest.mark.parametrize("mode", [FastPathMode.OFF, FastPathMode.SHADOW])
def test_off_and_shadow_never_reach_the_composite_steps(
    monkeypatch: pytest.MonkeyPatch, mode: FastPathMode
) -> None:
    world = World(monkeypatch, Runner(), mode)

    world.run()

    assert world.steps == []
    assert [r.profile_id for r in world.started] == [SALES], "today's route still runs"
    assert world.runner.calls == []


def test_a_composite_recovered_after_routing_was_turned_off_falls_back_as_not_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch, Runner(), FastPathMode.ON)
    composite = CompositeCall((CompositeMember("order_plan", STOCK, 1),))

    async def replayed(request: TurnRequest) -> Any:
        return RoutedTurn(request, None, None, composite)

    monkeypatch.setattr(turn_workflow, "_step_route", replayed)
    deps = turn_workflow._dependencies  # noqa: SLF001
    assert deps is not None
    monkeypatch.setattr(turn_workflow, "_dependencies", dataclasses.replace(deps, run_intent=None))

    outcome = world.run()

    assert outcome.result is not None and outcome.result.text == "model path"
    assert world.steps == ["_step_composite_run", "_step_composite_fallback"]
    assert world.planned() == [f"fallback:{PLAN_NOT_WIRED}"]


def test_the_composite_steps_are_durable_steps() -> None:
    for name in STEPS:
        step = getattr(turn_workflow, name)
        assert inspect.unwrap(step) is not step, name
