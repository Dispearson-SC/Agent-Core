"""The turn router on a REAL Postgres and a REAL DBOS (D95).

Phase:   Phase 5 (fast communication), P1
Covers:  persistence_pg/turn_routes_migration.py, persistence_pg/route_audit.py,
         turn_workflow._step_route across a process death

WHAT IS BEING DEFENDED

    1. THE AUDIT ROW. A consultation is one `audit_turn_routes` row carrying the router, both
       profiles, the decision (NULL when the router answered nothing), the label, confidence,
       calibration, model, elapsed time and fallback reason.
    2. A ROUTE SURVIVES A CRASH UNCHANGED. The process dies after the route step committed and
       while the routed profile's turn is running. The next process recovers the workflow, reads
       the route back from the operation log and finishes the turn as the routed profile, although
       the router would now answer `pass`: it is asked exactly once.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.events_pg.publisher import PgEventPublisher
from agent_core.adapters.driven.persistence_pg.route_audit import PgRouteAudit
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.application.route_turn import RouteTurn
from agent_core.domain.intents import IntentRecord
from agent_core.domain.routing import (
    FALLBACK_TIMEOUT,
    RouteDecision,
    RouteKind,
    RouteRecord,
    RouteRequest,
    RoutingPolicy,
)
from agent_core.domain.turn import TurnId, delegated_identity
from tests.fakes import peer_world as w
from tests.fakes.delegation import allowing_policy

_DATABASE = "agent_core_route_audit_test"

pytestmark = pytest.mark.skipif(not w.postgres_reachable(), reason="no reachable Postgres instance")


def _connect() -> Any:
    return psycopg.connect(w.conninfo(_DATABASE))


def _routes() -> list[tuple[Any, ...]]:
    with _connect() as conn:
        return conn.execute(
            "SELECT turn_id, tenant_id, router, requested_profile, profile_id, decision, label, "
            "confidence, calibrated, model, intent, elapsed_ms, fallback_reason, reason, "
            "input_tokens, output_tokens, actor, on_behalf_of FROM audit_turn_routes ORDER BY id"
        ).fetchall()


def test_a_consultation_is_one_audit_row_with_its_evidence() -> None:
    async def scenario() -> None:
        await w.fresh_databases(_DATABASE)
        audit = PgRouteAudit(_connect)
        await audit.record_route(
            TurnId("turn-1"),
            "t",
            RouteRecord(
                router="keyword", requested_profile="front", profile_id="billing",
                decision=RouteKind.ROUTE, elapsed_ms=12, label="billing", confidence=0.9,
                calibrated=True, model="clf-1", reason="confident", input_tokens=318,
                output_tokens=34, actor="peer-agent", on_behalf_of="u",
            ),
        )
        await audit.record_route(
            TurnId("turn-2"),
            "t",
            RouteRecord(
                router="keyword", requested_profile="front", profile_id="front",
                decision=None, elapsed_ms=1500, fallback_reason=FALLBACK_TIMEOUT,
            ),
        )

    asyncio.run(scenario())

    assert _routes() == [
        ("turn-1", "t", "keyword", "front", "billing", "route", "billing", 0.9, True, "clf-1",
         None, 12, None, "confident", 318, 34, "peer-agent", "u"),
        ("turn-2", "t", "keyword", "front", "front", None, None, None, False, None,
         None, 1500, "timeout", None, None, None, None, None),
    ]


def test_a_fast_path_consultation_records_its_mode_verdict_intents_and_every_answer() -> None:
    """S10 (migration 0049): the fast path's columns, NULL on a row written with it off."""

    async def scenario() -> None:
        await w.fresh_databases(_DATABASE)
        audit = PgRouteAudit(_connect)
        await audit.record_route(
            TurnId("turn-1"),
            "t",
            RouteRecord(
                router="jev", requested_profile="front", profile_id="front",
                decision=RouteKind.ROUTE, elapsed_ms=90, fast_path_mode="shadow",
                fast_path="composite", fast_path_intents="day_summary,order_plan",
                assessment="intents=day_summary:0.93,order_plan:0.81;action=0.02;"
                "follow_up=-;complexity=0.10",
            ),
        )
        await audit.record_route(
            TurnId("turn-2"),
            "t",
            RouteRecord(
                router="jev", requested_profile="front", profile_id="front",
                decision=RouteKind.PASS, elapsed_ms=80,
            ),
        )

    asyncio.run(scenario())

    with _connect() as conn:
        rows = conn.execute(
            "SELECT turn_id, fast_path_mode, fast_path, fast_path_intents, assessment "
            "FROM audit_turn_routes ORDER BY id"
        ).fetchall()
    assert rows == [
        ("turn-1", "shadow", "composite", "day_summary,order_plan",
         "intents=day_summary:0.93,order_plan:0.81;action=0.02;follow_up=-;complexity=0.10"),
        ("turn-2", None, None, None, None),
    ]


def test_an_intent_plan_is_one_audit_row_with_its_outcome() -> None:
    """D97: `audit_turn_intents` (migration 0047) holds whether the plan answered, its tool count,
    reply mode and fallback reason; never an argument or a result."""

    async def scenario() -> None:
        await w.fresh_databases(_DATABASE)
        audit = PgRouteAudit(_connect)
        await audit.record_intent(
            TurnId("turn-1"),
            "t",
            IntentRecord(
                "stock", "shop", True, 2, "compose", None, actor="peer-agent", on_behalf_of="u"
            ),
        )
        await audit.record_intent(
            TurnId("turn-2"), "t", IntentRecord("stock", "shop", False, 1, "template", "tool_error")
        )

    asyncio.run(scenario())

    with _connect() as conn:
        rows = conn.execute(
            "SELECT turn_id, tenant_id, intent, profile_id, plan_used, tool_count, reply_mode, "
            "fallback_reason, actor, on_behalf_of FROM audit_turn_intents ORDER BY id"
        ).fetchall()
    assert rows == [
        ("turn-1", "t", "stock", "shop", True, 2, "compose", None, "peer-agent", "u"),
        ("turn-2", "t", "stock", "shop", False, 1, "template", "tool_error", None, None),
    ]


class CountingRouter:
    def __init__(self, decision: RouteDecision) -> None:
        self.decision = decision
        self.calls = 0

    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        self.calls += 1
        return self.decision


class FlippingGate:
    """A peer gate that allows the hop until told otherwise: a recovered turn must not ask it."""

    def __init__(self) -> None:
        self.allow = True
        self.calls = 0

    def __call__(self, entry: Any, target: Any, hop: int) -> tuple[int | None, str | None]:
        self.calls += 1
        return (hop + 1, None) if self.allow else (None, "caller_does_not_allow_callee")


def _bind_routing(
    monkeypatch: pytest.MonkeyPatch, router: CountingRouter, gate: FlippingGate
) -> None:
    profiles = w.profiles()
    profiles[w.A] = dataclasses.replace(
        profiles[w.A], routing=RoutingPolicy(router="keyword", targets=(w.B,))
    )
    deps = turn_workflow._dependencies  # noqa: SLF001
    assert deps is not None
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        dataclasses.replace(
            deps,
            events=PgEventPublisher(w.conninfo(_DATABASE)),
            route_turn=RouteTurn(
                routers=router,
                audit=PgRouteAudit(_connect),
                profiles=profiles,
                peer_gate=gate,
                policy=allowing_policy(),
            ),
        ),
    )


def test_a_route_recorded_before_a_crash_is_replayed_and_never_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = w.bind(monkeypatch, _DATABASE)
    world.start.b_answers_directly = True
    world.start.b_hangs = True  # the routed turn is running when the process dies
    router = CountingRouter(RouteDecision(kind=RouteKind.ROUTE, profile_id=w.B, label="b"))
    gate = FlippingGate()
    _bind_routing(monkeypatch, router, gate)
    turn = str(w.ASKING_TURN)
    seen: dict[str, Any] = {}

    async def first_process() -> None:
        async with w.Dbos(_DATABASE):
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            await w.until_step_recorded(turn, "_step_route")
            await w.until(lambda: world.start.starts(w.B) == 1, "the routed turn to start")

    async def second_process() -> None:
        world.start.b_hangs = False
        router.decision = RouteDecision(kind=RouteKind.PASS)  # it would answer differently now
        gate.allow = False  # and the delegation would now be refused
        async with w.Dbos(_DATABASE, drop=False):
            seen["outcome"] = await w.result_of(turn)

    with w.dying_process(monkeypatch):
        asyncio.run(first_process())
    asyncio.run(second_process())

    assert router.calls == 1, "the route is read back from the log, never re-classified"
    assert gate.calls == 1, "the delegation is read back too, never re-evaluated (S8)"
    assert {r.caller for r in world.start.requests} == {delegated_identity(w.request().caller)}
    assert [r.profile_id for r in world.start.requests] == [w.B, w.B]
    assert world.start.starts(w.A) == 0
    assert seen["outcome"].result is not None
    assert [(r[3], r[4], r[5]) for r in _routes()] == [(w.A, w.B, "route")]
    with _connect() as conn:
        events = conn.execute(
            "SELECT kind, agent, detail FROM turn_events WHERE turn_id = %s ORDER BY seq", (turn,)
        ).fetchall()
    assert events[0] == ("routed", w.B, "routed")
    assert [e[0] for e in events].count("routed") == 1
