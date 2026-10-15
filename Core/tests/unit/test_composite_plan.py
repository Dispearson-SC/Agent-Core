"""Running a composite plan (Phase 5, S10): every detected intent's plan, then ONE compose.

Subject: application/run_intent.py (`RunIntent.run_composite`, `.reply_composite`,
`.fall_back_composite`).

THE RULES UNDER TEST.
  * `run_composite` resolves every member's plan, then runs ALL their calls concurrently, each as
    its owner (delegated, S8, when the member carries a hop) through the runner; identical calls
    of one owner run once. The results come back grouped by intent, in intent order, each with its
    own step id. Any failure (a plan that cannot run, a failed call, more calls than the budget) is
    the fallback reason; the first one in deterministic order wins.
  * `reply_composite` writes ONE reply on the composer profile (the entry profile unless one is
    configured) from all the groups, and only then persists the turn (the person's request, the
    reply, the turn end) and audits every member as used (`composite`). A compose that fails
    persists nothing.
  * `fall_back_composite` audits every member as not used, with the reason.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from agent_core.application.run_intent import RunIntent
from agent_core.domain.fast_path import CompositeCall, CompositeMember, CompositeRun
from agent_core.domain.intents import (
    PLAN_ARGUMENT_INVALID,
    PLAN_OVER_BUDGET,
    PLAN_TOOL_ERROR,
    ArgumentSource,
    IntentPlan,
    IntentRecord,
    PlannedCall,
    PlanStep,
    PlanToolResult,
    SourceKind,
)
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    Usage,
    UserInput,
)
from tests.fakes.ports import FakeAuditSink, FakeConversationStore

TURN = TurnId("turn-1")
FRONT, SALES, STOCK, WRITER = "front", "sales", "stock", "writer"

PLANS = {
    "day_summary": IntentPlan(
        "day_summary", SALES, (PlanStep("snap", "get_snapshot"), PlanStep("issues", "get_issues"))
    ),
    "order_plan": IntentPlan("order_plan", STOCK, (PlanStep("plan", "get_order_plan"),)),
    "low_stock": IntentPlan(
        "low_stock",
        STOCK,
        (
            PlanStep(
                "inv", "get_inventory",
                (("day", ArgumentSource(SourceKind.CONTEXT, name="today")),),
            ),
            PlanStep("plan", "get_order_plan"),  # the same call order_plan makes
        ),
    ),
    "needs_sku": IntentPlan(
        "needs_sku", STOCK,
        (PlanStep("one", "get_item", (("sku", ArgumentSource(SourceKind.ARGUMENT, name="sku")),)),),
    ),
}  # fmt: skip

BOTH = CompositeCall(
    (CompositeMember("order_plan", STOCK, 1), CompositeMember("day_summary", SALES, 1))
)


def _request(text: str = "how am I doing and what do I order?") -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("case-1"), tenant_id=TenantId("t")),
        caller=CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t")),
        profile_id=FRONT,
        input=UserInput(text=text),
    )


class Runner:
    def __init__(self, *, failing: str | None = None, compose_error: bool = False) -> None:
        self.failing = failing
        self.compose_error = compose_error
        self.calls: list[tuple[str, str, str, int, PlannedCall]] = []
        self.composed: list[tuple[str, Any]] = []
        self.in_flight = 0
        self.peak = 0

    async def call_tool(
        self, turn_id: TurnId, request: TurnRequest, profile: AgentProfile, call: PlannedCall
    ) -> PlanToolResult:
        self.calls.append(
            (profile.id, request.profile_id, request.caller.subject_id, request.hop, call)
        )
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        if call.tool == self.failing:
            return PlanToolResult(call.step_id, call.tool, failure=PLAN_TOOL_ERROR)
        return PlanToolResult(call.step_id, call.tool, content=f"{call.tool}=1")

    async def compose_composite(
        self, turn_id: TurnId, request: TurnRequest, profile: AgentProfile, groups: Any
    ) -> TurnOutcome:
        if self.compose_error:
            raise RuntimeError("provider down")
        self.composed.append((profile.id, groups))
        return TurnOutcome(
            turn_id=turn_id,
            messages=("composed",),
            result=TurnResult(text="Both answers.", usage=Usage(input_tokens=9, output_tokens=3)),
        )

    def template_reply(self, turn_id: TurnId, text: str) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, messages=(text,), result=TurnResult(text=text))


class Routes:
    def __init__(self) -> None:
        self.intents: list[IntentRecord] = []

    async def record_intent(self, turn_id: TurnId, tenant_id: str, record: IntentRecord) -> None:
        self.intents.append(record)


class Context:
    def update_from_response(self, session: SessionRef, usage: Usage) -> None:
        return None


class World:
    def __init__(self, runner: Runner, **kwargs: Any) -> None:
        self.runner = runner
        self.store = FakeConversationStore()
        self.audit = FakeAuditSink()
        self.routes = Routes()
        self.use_case = RunIntent(
            runner=runner,  # type: ignore[arg-type]
            store=self.store,
            audit=self.audit,
            routes=self.routes,  # type: ignore[arg-type]
            context=Context(),  # type: ignore[arg-type]
            profiles={
                FRONT: AgentProfile(id=FRONT, persona="f", model="m"),
                SALES: AgentProfile(id=SALES, persona="s", model="m"),
                STOCK: AgentProfile(id=STOCK, persona="k", model="m", max_iterations=2),
                WRITER: AgentProfile(id=WRITER, persona="w", model="m"),
            },
            plans=PLANS,
            today=lambda: date(2026, 10, 15),
            **kwargs,
        )

    def run(self, composite: CompositeCall = BOTH) -> CompositeRun:
        result: CompositeRun = asyncio.run(
            self.use_case.run_composite(TURN, _request(), composite)
        )
        return result


# --- running every plan ----------------------------------------------------------------------


def test_every_members_calls_run_concurrently_as_their_owner_delegated() -> None:
    runner = Runner()

    run = World(runner).run()

    assert run.fallback is None and run.tool_count == 3
    assert runner.peak == 3, "all calls are in flight together"
    assert sorted((c[0], c[1], c[2], c[3], c[4].tool) for c in runner.calls) == [
        (SALES, SALES, "peer-agent", 1, "get_issues"),
        (SALES, SALES, "peer-agent", 1, "get_snapshot"),
        (STOCK, STOCK, "peer-agent", 1, "get_order_plan"),
    ]
    assert run.groups == (
        (
            "day_summary",
            (
                PlanToolResult("snap", "get_snapshot", content="get_snapshot=1"),
                PlanToolResult("issues", "get_issues", content="get_issues=1"),
            ),
        ),
        ("order_plan", (PlanToolResult("plan", "get_order_plan", content="get_order_plan=1"),)),
    )


def test_calls_are_dispatched_in_a_deterministic_order() -> None:
    runner = Runner()
    World(runner).run()
    assert [c[4].tool for c in runner.calls] == ["get_issues", "get_snapshot", "get_order_plan"]


def test_an_owner_that_is_the_entry_profile_runs_as_the_person() -> None:
    runner = Runner()
    plans = {**PLANS, "own": IntentPlan("own", FRONT, (PlanStep("x", "get_x"),))}
    world = World(runner)
    world.use_case = RunIntent(
        runner=runner,  # type: ignore[arg-type]
        store=world.store,
        audit=world.audit,
        routes=world.routes,  # type: ignore[arg-type]
        context=Context(),  # type: ignore[arg-type]
        profiles={FRONT: AgentProfile(id=FRONT, persona="f", model="m")},
        plans=plans,
    )
    run = world.run(CompositeCall((CompositeMember("own", FRONT),)))
    assert run.fallback is None
    assert runner.calls[0][1:4] == (FRONT, "u", 0)


def test_identical_calls_of_one_owner_run_once_and_each_intent_gets_its_result() -> None:
    runner = Runner()
    composite = CompositeCall(
        (CompositeMember("order_plan", STOCK, 1), CompositeMember("low_stock", STOCK, 1))
    )

    run = World(runner).run(composite)

    assert sorted(c[4].tool for c in runner.calls) == ["get_inventory", "get_order_plan"]
    assert run.tool_count == 2
    assert dict(run.groups)["low_stock"] == (
        PlanToolResult("inv", "get_inventory", content="get_inventory=1"),
        PlanToolResult("plan", "get_order_plan", content="get_order_plan=1"),
    )
    assert dict(run.groups)["order_plan"] == (
        PlanToolResult("plan", "get_order_plan", content="get_order_plan=1"),
    )
    assert runner.calls[0][4].arguments in ({"day": "2026-10-15"}, {})


def test_a_failed_call_is_the_fallback() -> None:
    run = World(Runner(failing="get_order_plan")).run()
    assert run.fallback == PLAN_TOOL_ERROR and run.groups == ()


def test_a_plan_that_needs_an_extracted_argument_cannot_run_in_a_composite() -> None:
    runner = Runner()
    run = World(runner).run(CompositeCall((CompositeMember("needs_sku", STOCK, 1),)))
    assert run.fallback == PLAN_ARGUMENT_INVALID and runner.calls == []


def test_more_calls_than_an_owners_budget_or_the_composite_budget_is_over_budget() -> None:
    heavy = IntentPlan(
        "heavy", STOCK, tuple(PlanStep(f"s{i}", f"get_{i}") for i in range(3))
    )  # three calls, the owner allows two
    world = World(Runner())
    world.use_case._plans = {**PLANS, "heavy": heavy}  # noqa: SLF001
    assert world.run(CompositeCall((CompositeMember("heavy", STOCK, 1),))).fallback == (
        PLAN_OVER_BUDGET
    )
    tight = World(Runner(), max_composite_calls=2)
    assert tight.run().fallback == PLAN_OVER_BUDGET


def test_a_member_whose_plan_moved_to_another_owner_falls_back() -> None:
    moved = CompositeCall((CompositeMember("order_plan", SALES, 1),))
    run = World(Runner()).run(moved)
    assert run.fallback is not None and run.fallback.startswith("error:")


# --- the one reply ---------------------------------------------------------------------------


def _reply(world: World, run: CompositeRun, composite: CompositeCall = BOTH) -> Any:
    return asyncio.run(world.use_case.reply_composite(TURN, _request(), composite, run))


def test_the_reply_is_one_compose_on_the_entry_profile_from_every_group_then_persisted() -> None:
    world = World(Runner())
    run = world.run()

    outcome = _reply(world, run)

    assert outcome is not None and outcome.result.text == "Both answers."
    assert world.runner.composed == [(FRONT, run.groups)]
    assert world.store.requests == [(TURN, _request())], "the person's own request"
    assert [o.result.text for _, o in world.store.outcomes] == ["Both answers."]
    assert world.routes.intents == [
        IntentRecord("day_summary", SALES, True, 2, "composite", None, "peer-agent", "u"),
        IntentRecord("order_plan", STOCK, True, 1, "composite", None, "peer-agent", "u"),
    ]


def test_a_configured_composer_writes_the_reply() -> None:
    world = World(Runner(), composer=WRITER)
    _reply(world, world.run())
    assert world.runner.composed[0][0] == WRITER


def test_a_compose_that_fails_persists_nothing() -> None:
    world = World(Runner(compose_error=True))
    run = world.run()
    assert _reply(world, run) is None
    assert world.store.requests == [] and world.store.outcomes == []
    assert world.routes.intents == []


def test_a_fallback_audits_every_member_as_not_used() -> None:
    world = World(Runner())
    asyncio.run(world.use_case.fall_back_composite(TURN, _request(), BOTH, PLAN_TOOL_ERROR, 1))
    who = ("peer-agent", "u")
    assert world.routes.intents == [
        IntentRecord("day_summary", SALES, False, 1, "composite", PLAN_TOOL_ERROR, *who),
        IntentRecord("order_plan", STOCK, False, 1, "composite", PLAN_TOOL_ERROR, *who),
    ]


# --- the answer cache (S10, 5.8) ---------------------------------------------------------------

from dataclasses import replace as _replace  # noqa: E402

from agent_core.domain.answer_cache import AnswerCacheKey, CachedAnswer  # noqa: E402
from agent_core.domain.fast_path_cache import composite_cache_key  # noqa: E402


class Cache:
    """An `AnswerCache`: a dict, with the TTL each entry was stored for."""

    def __init__(self, *, broken: bool = False) -> None:
        self.entries: dict[str, tuple[CachedAnswer, int]] = {}
        self.broken = broken

    async def get(self, key: AnswerCacheKey) -> CachedAnswer | None:
        if self.broken:
            raise RuntimeError("cache down")
        entry = self.entries.get(key.digest())
        return None if entry is None else entry[0]

    async def put(self, key: AnswerCacheKey, answer: CachedAnswer, ttl_seconds: int) -> None:
        if self.broken:
            raise RuntimeError("cache down")
        self.entries[key.digest()] = (answer, ttl_seconds)

    async def invalidate(self, tenant_id: str, target: str | None = None) -> int:
        return 0


def _cached_world(cache: Cache, runner: Runner | None = None) -> World:
    return World(runner or Runner(), cache=cache, cache_ttl_seconds=300)


def test_a_composite_reply_is_remembered_and_a_second_ask_answers_without_any_call() -> None:
    cache = Cache()
    first = _cached_world(cache)
    _reply(first, first.run())
    assert len(cache.entries) == 1 and next(iter(cache.entries.values()))[1] == 300

    runner = Runner()
    second = _cached_world(cache, runner)
    run = second.run()

    assert run.cached == "Both answers." and runner.calls == [] and run.groups == ()
    outcome = _reply(second, run)
    assert outcome.result.text == "Both answers." and runner.composed == []
    assert [o.result.text for _, o in second.store.outcomes] == ["Both answers."]
    assert {r.reply_mode for r in second.routes.intents} == {"cache"}


def test_the_cache_key_is_tenant_case_data_version_intents_and_the_normalised_question() -> None:
    profile = AgentProfile(id=FRONT, persona="f", model="m")
    base = composite_cache_key(_request(), BOTH, profile)
    shouted = _request("  How am I doing and what do I ORDER  ")
    assert base == composite_cache_key(shouted, BOTH, profile)
    other_case = _replace(_request(), session=SessionRef(SessionId("case-2"), TenantId("t")))
    newer_data = _replace(_request(), data_version="2026-10-16")
    one_intent = CompositeCall((CompositeMember("order_plan", STOCK, 1),))
    for different in (
        composite_cache_key(other_case, BOTH, profile),
        composite_cache_key(newer_data, BOTH, profile),
        composite_cache_key(_request("and tomorrow?"), BOTH, profile),
        composite_cache_key(_request(), one_intent, profile),
        composite_cache_key(_request(), BOTH, _replace(profile, persona="changed")),
    ):
        assert different.digest() != base.digest()
    assert base.tenant_id == "t" and base.target == "fast_path:day_summary,order_plan"


def test_a_cache_that_is_down_is_a_miss_and_never_fails_the_turn() -> None:
    world = _cached_world(Cache(broken=True))
    run = world.run()
    assert run.cached is None and run.fallback is None
    assert _reply(world, run) is not None


def test_without_a_cache_nothing_is_remembered() -> None:
    world = World(Runner())
    assert world.run().cached is None
