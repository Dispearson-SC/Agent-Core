"""Running an intent plan (D97, S3/S6 layer B): the use case, with fakes.

Subject: application/run_intent.py (`RunIntent`).

THE RULES UNDER TEST. `prepare` resolves every step's arguments up front (the router's, their
defaults, constants and the context values) and refuses a plan that cannot run: an argument the
router did not extract, more calls than the owning profile's budget, a plan for another profile.
`call` runs one call through the runner, bounded by a timeout; a raise or a timeout is a failed
call, never a failed turn. `reply` writes the answer (one compose call, or the template) and only
then persists the turn (the request, the reply, the turn-end audit) and audits the plan as used; a
reply that fails persists nothing, so the normal path can start the turn from scratch.
`fall_back` audits the plan as not used with its reason.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from agent_core.application.run_intent import PlanReplyNotPersistedError, RunIntent
from agent_core.domain.intents import (
    PLAN_ARGUMENT_INVALID,
    PLAN_OVER_BUDGET,
    PLAN_TIMEOUT,
    PLAN_TOOL_ERROR,
    ArgumentSource,
    IntentCall,
    IntentPlan,
    IntentRecord,
    PlannedCall,
    PlanStep,
    PlanToolResult,
    ReplyMode,
    SourceKind,
)
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    PEER_SUBJECT_ID,
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
    delegated_identity,
)
from tests.fakes.ports import FakeAuditSink, FakeConversationStore

TURN = TurnId("turn-1")


def _request(profile: str = "billing") -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t")),
        caller=CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t")),
        profile_id=profile,
        input=UserInput(text="how much for 3 kg?"),
    )


def _plan(reply: ReplyMode = ReplyMode.COMPOSE, *, steps: int = 2) -> IntentPlan:
    first = PlanStep(
        id="quote",
        tool="pricing_quote",
        arguments=(
            ("weight", ArgumentSource(SourceKind.ARGUMENT, name="weight")),
            ("zone", ArgumentSource(SourceKind.CONSTANT, value="north")),
            ("day", ArgumentSource(SourceKind.CONTEXT, name="today")),
        ),
    )
    rest = tuple(PlanStep(id=f"s{i}", tool="orders_lookup") for i in range(steps - 1))
    return IntentPlan(
        intent="quote",
        profile_id="billing",
        steps=(first, *rest),
        reply=reply,
        template="Quote {quote}" if reply is ReplyMode.TEMPLATE else None,
    )


class Runner:
    """An `IntentRunner` that answers scripted results and records what it was asked."""

    def __init__(
        self,
        *,
        results: dict[str, PlanToolResult] | None = None,
        sleep: float = 0.0,
        error: Exception | None = None,
        compose_error: Exception | None = None,
    ) -> None:
        self.results = results or {}
        self.sleep = sleep
        self.error = error
        self.compose_error = compose_error
        self.calls: list[tuple[str, PlannedCall]] = []
        self.composed: list[tuple[str, tuple[PlanToolResult, ...]]] = []

    async def call_tool(
        self, turn_id: TurnId, request: TurnRequest, profile: AgentProfile, call: PlannedCall
    ) -> PlanToolResult:
        self.calls.append((profile.id, call))
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.error is not None:
            raise self.error
        return self.results.get(
            call.step_id, PlanToolResult(call.step_id, call.tool, content=f"{call.tool}-ok")
        )

    async def compose_reply(
        self,
        turn_id: TurnId,
        request: TurnRequest,
        profile: AgentProfile,
        results: tuple[PlanToolResult, ...],
    ) -> TurnOutcome:
        if self.compose_error is not None:
            raise self.compose_error
        self.composed.append((profile.id, results))
        return TurnOutcome(
            turn_id=turn_id,
            messages=("composed-message",),
            result=TurnResult(text="It costs 9.", usage=Usage(input_tokens=50, output_tokens=7)),
        )

    def template_reply(self, turn_id: TurnId, text: str) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, messages=(f"msg:{text}",), result=TurnResult(text=text))


class Routes:
    def __init__(self) -> None:
        self.intents: list[tuple[TurnId, str, IntentRecord]] = []

    async def record_route(self, *args: Any) -> None:  # pragma: no cover - not used here
        raise AssertionError("RunIntent never audits a route")

    async def record_intent(self, turn_id: TurnId, tenant_id: str, record: IntentRecord) -> None:
        self.intents.append((turn_id, tenant_id, record))


class Context:
    def __init__(self) -> None:
        self.updates: list[Usage] = []

    def update_from_response(self, session: SessionRef, usage: Usage) -> None:
        self.updates.append(usage)


class World:
    def __init__(self, runner: Runner, plan: IntentPlan | None = None, **kwargs: Any) -> None:
        self.runner = runner
        self.store = FakeConversationStore()
        self.audit = FakeAuditSink()
        self.routes = Routes()
        self.context = Context()
        self.profile = AgentProfile(id="billing", persona="b", model="m", max_iterations=3)
        self.use_case = RunIntent(
            runner=runner,
            store=self.store,
            audit=self.audit,
            routes=self.routes,
            context=self.context,  # type: ignore[arg-type]
            profiles={"billing": self.profile},
            plans={"quote": plan or _plan()},
            today=lambda: date(2026, 10, 14),
            **kwargs,
        )


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


# --- prepare -------------------------------------------------------------------------------------


def test_prepare_resolves_every_call_up_front_from_the_router_constants_and_context() -> None:
    world = World(Runner())

    prepared = _run(world.use_case.prepare(TURN, _request(), IntentCall("quote", {"weight": 3})))

    assert prepared.fallback is None and prepared.reply is ReplyMode.COMPOSE
    assert prepared.calls == (
        PlannedCall("quote", "pricing_quote", {"weight": 3, "zone": "north", "day": "2026-10-14"}),
        PlannedCall("s0", "orders_lookup", {}),
    )


def test_prepare_refuses_a_missing_argument_an_over_budget_plan_and_another_profile() -> None:
    world = World(Runner())
    missing = _run(world.use_case.prepare(TURN, _request(), IntentCall("quote", {})))
    assert (missing.fallback, missing.calls) == (PLAN_ARGUMENT_INVALID, ())

    heavy = World(Runner(), _plan(steps=4))  # four calls, the profile allows three iterations
    over = _run(heavy.use_case.prepare(TURN, _request(), IntentCall("quote", {"weight": 1})))
    assert over.fallback == PLAN_OVER_BUDGET

    elsewhere = _run(
        world.use_case.prepare(TURN, _request("front"), IntentCall("quote", {"weight": 1}))
    )
    assert elsewhere.fallback is not None and elsewhere.fallback.startswith("error:")


# --- one call ------------------------------------------------------------------------------------


def test_a_call_runs_as_the_owning_profile_through_the_runner() -> None:
    runner = Runner()
    world = World(runner)
    planned = PlannedCall("quote", "pricing_quote", {"weight": 3})

    result = _run(world.use_case.call(TURN, _request(), planned))

    assert result == PlanToolResult("quote", "pricing_quote", content="pricing_quote-ok")
    assert runner.calls == [("billing", planned)]


def test_a_call_that_raises_or_outlives_its_timeout_is_a_failed_call_not_a_failed_turn() -> None:
    planned = PlannedCall("quote", "pricing_quote", {})
    raised = _run(
        World(Runner(error=RuntimeError("secret backend text"))).use_case.call(
            TURN, _request(), planned
        )
    )
    slow = _run(
        World(Runner(sleep=1.0), tool_timeout_ms=10).use_case.call(TURN, _request(), planned)
    )

    assert raised.failure == "error:RuntimeError" and "secret" not in raised.content
    assert slow.failure == PLAN_TIMEOUT


# --- the reply -----------------------------------------------------------------------------------


def test_a_composed_reply_persists_the_turn_and_audits_the_plan_as_used() -> None:
    runner = Runner()
    world = World(runner)
    results = (
        PlanToolResult("quote", "pricing_quote", content="9 USD"),
        PlanToolResult("s0", "orders_lookup", content="[]"),
    )

    outcome = _run(world.use_case.reply(TURN, _request(), IntentCall("quote"), results))

    assert outcome is not None and outcome.result.text == "It costs 9."
    assert runner.composed == [("billing", results)]
    assert world.store.requests == [(TURN, _request())]
    assert world.store.outcomes == [(TURN, outcome)]
    assert [c.kind for c in world.audit.calls] == ["turn_end"]
    assert world.context.updates == [outcome.result.usage]
    assert world.routes.intents == [
        (TURN, "t", IntentRecord("quote", "billing", True, 2, "compose", None)),
    ]


def test_a_template_reply_writes_the_results_in_without_a_model_call() -> None:
    runner = Runner()
    world = World(runner, _plan(ReplyMode.TEMPLATE, steps=1))
    results = (PlanToolResult("quote", "pricing_quote", content="9 USD"),)

    outcome = _run(world.use_case.reply(TURN, _request(), IntentCall("quote"), results))

    assert outcome.result.text == "Quote 9 USD"
    assert outcome.messages == ("msg:Quote 9 USD",)
    assert runner.composed == []
    assert world.routes.intents[0][2].reply_mode == "template"


def test_a_reply_that_fails_persists_nothing_so_the_normal_path_starts_clean() -> None:
    world = World(Runner(compose_error=RuntimeError("provider down")))

    outcome = _run(
        world.use_case.reply(
            TURN, _request(), IntentCall("quote"), (PlanToolResult("quote", "t", content="x"),)
        )
    )

    assert outcome is None
    assert world.store.requests == [] and world.store.outcomes == []
    assert world.audit.calls == [] and world.routes.intents == []


async def _down(*args: Any) -> None:
    raise RuntimeError("database down")


def _one_result() -> tuple[PlanToolResult, ...]:
    return (PlanToolResult("quote", "t", content="x"),)


def test_a_request_that_cannot_be_persisted_falls_back_with_nothing_written() -> None:
    """Slice 36: the request write is one transaction, so when it fails nothing of the turn exists
    and the normal path can start it from scratch; no turn end, no plan audited as used."""
    world = World(Runner())
    world.store.append_request = _down  # type: ignore[method-assign]

    outcome = _run(world.use_case.reply(TURN, _request(), IntentCall("quote"), _one_result()))

    assert outcome is None
    assert world.store.outcomes == []
    assert world.audit.calls == [] and world.routes.intents == []
    assert world.context.updates == []


def test_a_delegated_plan_audits_both_identities() -> None:
    """S8: a plan the router sent to its owner runs delegated; its audit row names the subject it
    ran as and the person it acted for, used or fallen back."""
    world = World(Runner())
    request = replace(_request(), caller=delegated_identity(_request().caller))

    _run(world.use_case.reply(TURN, request, IntentCall("quote"), _one_result()))
    _run(world.use_case.fall_back(TURN, request, IntentCall("quote"), PLAN_TOOL_ERROR, 1))

    for _, _, record in world.routes.intents:
        assert (record.actor, record.on_behalf_of) == (PEER_SUBJECT_ID, "u")


def test_a_plan_run_as_the_person_names_no_delegation() -> None:
    world = World(Runner())

    _run(world.use_case.fall_back(TURN, _request(), IntentCall("quote"), PLAN_TOOL_ERROR, 1))

    assert (world.routes.intents[0][2].actor, world.routes.intents[0][2].on_behalf_of) == (
        None,
        None,
    )


def test_a_reply_that_cannot_be_persisted_after_its_request_fails_the_turn_cleanly() -> None:
    """Slice 36: the request is already written, so the normal path would write it twice; the
    reply step fails the turn with a named error instead, and nothing claims the plan answered."""
    world = World(Runner())
    world.store.append_outcome = _down  # type: ignore[method-assign]

    with pytest.raises(PlanReplyNotPersistedError, match="turn-1"):
        _run(world.use_case.reply(TURN, _request(), IntentCall("quote"), _one_result()))

    assert world.store.requests == [(TURN, _request())]
    assert world.audit.calls == [] and world.routes.intents == []


def test_a_turn_end_audit_that_fails_after_the_reply_is_persisted_never_fails_the_turn() -> None:
    """Slice 36: the reply is persisted, so the turn answered; the turn-end row and the context
    update are bookkeeping, logged when they fail, and the plan is still audited as used."""
    world = World(Runner())
    world.audit.record_turn_end = _down  # type: ignore[method-assign]

    def broken(*args: Any) -> None:
        raise RuntimeError("context down")

    world.context.update_from_response = broken  # type: ignore[method-assign]

    outcome = _run(world.use_case.reply(TURN, _request(), IntentCall("quote"), _one_result()))

    assert outcome is not None
    assert world.store.outcomes == [(TURN, outcome)]
    assert world.routes.intents[0][2].plan_used is True


def test_a_fallback_is_audited_with_its_reason_and_the_calls_made() -> None:
    world = World(Runner())

    _run(world.use_case.fall_back(TURN, _request(), IntentCall("quote"), PLAN_TOOL_ERROR, 1))

    assert world.routes.intents == [
        (TURN, "t", IntentRecord("quote", "billing", False, 1, "compose", PLAN_TOOL_ERROR)),
    ]


def test_an_audit_that_fails_never_fails_the_turn() -> None:
    world = World(Runner())

    async def broken(*args: Any) -> None:
        raise RuntimeError("audit down")

    world.routes.record_intent = broken  # type: ignore[method-assign]

    _run(world.use_case.fall_back(TURN, _request(), IntentCall("quote"), PLAN_TOOL_ERROR, 0))
    outcome = _run(
        world.use_case.reply(
            TURN, _request(), IntentCall("quote"), (PlanToolResult("quote", "t", content="x"),)
        )
    )
    assert outcome is not None


def test_the_tool_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="timeout"):
        World(Runner(), tool_timeout_ms=0)
