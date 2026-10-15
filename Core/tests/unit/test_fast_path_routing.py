"""The fast path switch in the route step: off, shadow and on (Phase 5, S10).

Subject: application/route_turn.py (`RouteTurn` with `fast_path`, `thresholds`, `recent_history`)
and the fast path fields of domain/routing.py (`RouteRequest`, `RouteDecision.assessment`,
`RouteRecord`).

THE RULES UNDER TEST.
  * off: the router is asked exactly what it was asked before (no extra questions, no history), a
    follow-up is not routed and its router is not asked, and the record carries no fast path.
  * shadow: the router is asked the extra questions (with the recent conversation when there is
    one); the verdict, the detected intents and every answer are audited, but the turn runs exactly
    what `off` would have run.
  * on: the verdict is applied. An action request, a message that depends on the conversation and
    a complex one run the entry profile; intents whose plans exist and whose owners the entry
    profile may ask become ONE composite (each member delegated as an ask_peer to its owner, S8);
    an intent without a plan or an owner it may not ask defers to the existing logic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from agent_core.application.route_turn import RouteTurn
from agent_core.domain.fast_path import (
    Assessment,
    CompositeCall,
    CompositeMember,
    FastPathMode,
    FastPathThresholds,
)
from agent_core.domain.intents import IntentPlan, PlanStep
from agent_core.domain.profile import AgentProfile
from agent_core.domain.routing import (
    FALLBACK_FOLLOW_UP,
    RouteDecision,
    RouteKind,
    RouteRecord,
    RouteRequest,
)
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes.delegation import delegation

FRONT = "front_desk"
SALES = "sales"
STOCK = "stock"
OUTSIDER = "outsider"  # owns a plan but is not a routing target of the front desk

PLANS = {
    "day_summary": IntentPlan("day_summary", SALES, (PlanStep("snap", "get_snapshot"),)),
    "order_plan": IntentPlan("order_plan", STOCK, (PlanStep("plan", "get_order_plan"),)),
    "secret": IntentPlan("secret", OUTSIDER, (PlanStep("s", "get_secret"),)),
}


def _profiles() -> dict[str, AgentProfile]:
    front = AgentProfile.from_mapping(
        {
            "id": FRONT,
            "persona": "p",
            "model": "m",
            "routing": {"router": "jev", "targets": [SALES, STOCK], "intents": True},
        }
    )
    return {
        FRONT: front,
        SALES: AgentProfile(id=SALES, persona="s", model="m"),
        STOCK: AgentProfile(id=STOCK, persona="k", model="m"),
        OUTSIDER: AgentProfile(id=OUTSIDER, persona="o", model="m"),
    }


def _request(text: str = "how am I doing today and what do I order?") -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("case-1"), tenant_id=TenantId("t")),
        caller=CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t")),
        profile_id=FRONT,
        input=UserInput(text=text),
    )


class Router:
    def __init__(self, decision: RouteDecision) -> None:
        self.decision = decision
        self.seen: list[RouteRequest] = []

    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        self.seen.append(request)
        return self.decision


class Routes:
    def __init__(self) -> None:
        self.records: list[RouteRecord] = []

    async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
        self.records.append(record)


def _assess(
    intents: dict[str, float],
    *,
    action: float = 0.02,
    follow_up: float | None = None,
    complexity: float = 0.1,
) -> Assessment:
    return Assessment.of(intents, action=action, follow_up=follow_up, complexity=complexity)


def _decision(
    assessment: Assessment | None, kind: RouteKind = RouteKind.ROUTE, **extra: Any
) -> RouteDecision:
    if kind is RouteKind.ROUTE:
        extra.setdefault("profile_id", SALES)
    return RouteDecision(kind=kind, label="x", confidence=0.8, assessment=assessment, **extra)


def _use_case(
    decision: RouteDecision,
    mode: FastPathMode,
    *,
    history: Sequence[str] | None = None,
    **seats: Any,
) -> tuple[RouteTurn, Router, Routes]:
    router, routes = Router(decision), Routes()

    async def has_history(session: SessionRef, turn_id: TurnId) -> bool:
        return bool(history)

    async def recent_history(session: SessionRef, turn_id: TurnId) -> tuple[str, ...]:
        return tuple(history or ())

    kwargs: dict[str, Any] = {**delegation(), **seats}
    use_case = RouteTurn(
        routers=router,
        audit=routes,
        profiles=_profiles(),
        plans=PLANS,
        has_history=has_history,
        recent_history=recent_history,
        fast_path=mode,
        thresholds=FastPathThresholds(),
        **kwargs,
    )
    return use_case, router, routes


def _run(use_case: RouteTurn) -> Any:
    return asyncio.run(use_case.execute(TurnId("turn-1"), _request()))


BOTH = _assess({"day_summary": 0.93, "order_plan": 0.81, "secret": 0.01})


# --- off ---------------------------------------------------------------------------------------


def test_off_asks_the_router_what_it_always_asked_and_records_no_fast_path() -> None:
    use_case, router, routes = _use_case(_decision(BOTH), FastPathMode.OFF)

    routed = _run(use_case)

    assert router.seen[0].fast_path is False and router.seen[0].history == ()
    assert routed.request.profile_id == SALES, "today's route still lands"
    assert routed.composite is None
    record = routes.records[0]
    assert (record.fast_path_mode, record.fast_path, record.assessment) == (None, None, None)
    assert record.outcome == "routed"


def test_off_does_not_ask_the_router_about_a_follow_up() -> None:
    use_case, router, routes = _use_case(_decision(BOTH), FastPathMode.OFF, history=["u: hi"])

    routed = _run(use_case)

    assert router.seen == []
    assert routed.request.profile_id == FRONT
    assert routes.records[0].fallback_reason == FALLBACK_FOLLOW_UP


def test_the_default_mode_is_off() -> None:
    router, routes = Router(_decision(BOTH)), Routes()
    use_case = RouteTurn(routers=router, audit=routes, profiles=_profiles(), **delegation())
    _run(use_case)
    assert router.seen[0].fast_path is False
    assert routes.records[0].fast_path_mode is None


# --- shadow ------------------------------------------------------------------------------------


def test_shadow_records_the_composite_it_would_run_but_runs_todays_route() -> None:
    use_case, router, routes = _use_case(_decision(BOTH), FastPathMode.SHADOW)

    routed = _run(use_case)

    assert router.seen[0].fast_path is True
    assert routed.request.profile_id == SALES and routed.composite is None
    record = routes.records[0]
    assert (record.fast_path_mode, record.fast_path) == ("shadow", "composite")
    assert record.fast_path_intents == "day_summary,order_plan"
    assert record.assessment == BOTH.summary()
    assert record.outcome == "routed", "shadow never changes what the turn does"


def test_shadow_asks_about_a_follow_up_with_the_conversation_but_still_does_not_route_it() -> None:
    history = ["person: how are sales?", "agent: 1,200 today."]
    decision = _decision(_assess({"order_plan": 0.9}, follow_up=0.8))
    use_case, router, routes = _use_case(decision, FastPathMode.SHADOW, history=history)

    routed = _run(use_case)

    assert router.seen[0].history == tuple(history)
    assert routed.request.profile_id == FRONT
    record = routes.records[0]
    assert record.fallback_reason == FALLBACK_FOLLOW_UP
    assert record.fast_path == "pass:depends_on_history"


# --- on ----------------------------------------------------------------------------------------


def test_on_turns_two_detected_intents_into_one_delegated_composite_on_the_entry_profile() -> None:
    use_case, _, routes = _use_case(_decision(BOTH), FastPathMode.ON)

    routed = _run(use_case)

    assert routed.request == _request(), "the turn stays the person's, on the entry profile"
    assert routed.intent is None
    assert routed.composite == CompositeCall(
        (CompositeMember("day_summary", SALES, 1), CompositeMember("order_plan", STOCK, 1))
    )
    record = routes.records[0]
    assert (record.fast_path_mode, record.fast_path) == ("on", "composite")
    assert record.profile_id == FRONT
    assert record.outcome == "composite"
    assert (record.actor, record.on_behalf_of) == ("peer-agent", "u")


def test_on_an_action_request_runs_the_entry_profile_even_when_the_choice_routed() -> None:
    decision = _decision(_assess({"order_plan": 0.9}, action=0.8))
    use_case, _, routes = _use_case(decision, FastPathMode.ON)

    routed = _run(use_case)

    assert routed.request == _request() and routed.composite is None
    assert routes.records[0].fast_path == "pass:action_request"
    assert routes.records[0].outcome == "passed"


def test_on_a_complex_message_runs_the_entry_profile() -> None:
    use_case, _, routes = _use_case(_decision(_assess({}, complexity=0.9)), FastPathMode.ON)
    routed = _run(use_case)
    assert routed.request.profile_id == FRONT
    assert routes.records[0].fast_path == "pass:complex"


def test_on_a_standalone_question_inside_a_conversation_may_be_a_composite() -> None:
    decision = _decision(_assess({"order_plan": 0.9}, follow_up=0.05))
    use_case, _, routes = _use_case(decision, FastPathMode.ON, history=["person: hi"])

    routed = _run(use_case)

    assert routed.composite is not None and routed.composite.intents == ("order_plan",)
    assert routes.records[0].fallback_reason is None


def test_on_a_follow_up_that_is_not_a_composite_is_still_not_routed() -> None:
    decision = _decision(_assess({}, follow_up=0.05))
    use_case, _, routes = _use_case(decision, FastPathMode.ON, history=["person: hi"])

    routed = _run(use_case)

    assert routed.request.profile_id == FRONT and routed.composite is None
    assert routes.records[0].fallback_reason == FALLBACK_FOLLOW_UP
    assert routes.records[0].fast_path == "defer:no_intent"


def test_on_without_intents_the_existing_route_logic_runs() -> None:
    use_case, _, routes = _use_case(_decision(_assess({"order_plan": 0.1})), FastPathMode.ON)
    routed = _run(use_case)
    assert routed.request.profile_id == SALES, "the choice's route still lands"
    assert routes.records[0].fast_path == "defer:no_intent"


def test_on_an_intent_whose_owner_is_not_a_target_defers_the_whole_composite() -> None:
    decision = _decision(_assess({"day_summary": 0.9, "secret": 0.9}))
    use_case, _, routes = _use_case(decision, FastPathMode.ON)

    routed = _run(use_case)

    assert routed.composite is None and routed.request.profile_id == SALES
    assert routes.records[0].fast_path == "defer:secret:target_not_allowed"


def test_on_an_intent_nobody_declared_a_plan_for_defers() -> None:
    decision = _decision(_assess({"day_summary": 0.9, "weather": 0.9}))
    use_case, _, routes = _use_case(decision, FastPathMode.ON)
    routed = _run(use_case)
    assert routed.composite is None
    assert routes.records[0].fast_path == "defer:weather:intent_unknown"


def test_on_an_owner_the_peer_gate_refuses_defers() -> None:
    def gate(entry: AgentProfile, target: AgentProfile, hop: int) -> tuple[int | None, str | None]:
        return (None, "callee_not_listening") if target.id == STOCK else (hop + 1, None)

    use_case, _, routes = _use_case(_decision(BOTH), FastPathMode.ON, peer_gate=gate)

    routed = _run(use_case)

    assert routed.composite is None
    assert routes.records[0].fast_path == "defer:order_plan:peer_refused:callee_not_listening"


def test_on_a_router_that_answered_no_assessment_runs_the_existing_logic() -> None:
    use_case, _, routes = _use_case(_decision(None), FastPathMode.ON)
    routed = _run(use_case)
    assert routed.request.profile_id == SALES
    assert routes.records[0].fast_path == "defer:unassessed"
    assert routes.records[0].assessment is None


def test_on_a_profile_without_intents_never_builds_a_composite() -> None:
    profiles = _profiles()
    no_intents = AgentProfile.from_mapping(
        {"id": FRONT, "persona": "p", "model": "m",
         "routing": {"router": "jev", "targets": [SALES, STOCK]}}
    )  # fmt: skip
    router, routes = Router(_decision(BOTH)), Routes()
    use_case = RouteTurn(
        routers=router,
        audit=routes,
        profiles={**profiles, FRONT: no_intents},
        plans=PLANS,
        fast_path=FastPathMode.ON,
        **delegation(),
    )
    routed = _run(use_case)
    assert routed.composite is None
    assert routes.records[0].fast_path == "defer:intent_not_supported"


def test_on_a_composite_that_cannot_be_audited_is_not_applied() -> None:
    class Down(Routes):
        async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
            raise RuntimeError("audit down")

    router = Router(_decision(BOTH))
    use_case = RouteTurn(
        routers=router, audit=Down(), profiles=_profiles(), plans=PLANS,
        fast_path=FastPathMode.ON, **delegation(),
    )  # fmt: skip
    routed = _run(use_case)
    assert routed.composite is None and routed.request.profile_id == FRONT


def test_a_routed_turn_recorded_by_an_older_build_reads_back_without_a_composite() -> None:
    """The route step's result is pickled into the operation log; a `.12` record has three slots."""
    from agent_core.application.route_turn import RoutedTurn

    old = RoutedTurn.__new__(RoutedTurn)
    old.__setstate__([_request(), None, None])

    assert old.composite is None and old.intent is None and old.request == _request()


def test_a_route_record_recorded_before_the_fast_path_reads_back_without_it() -> None:
    record = RouteRecord.__new__(RouteRecord)
    record.__setstate__(["jev", FRONT, FRONT, None, 5])
    assert (record.fast_path_mode, record.fast_path, record.assessment) == (None, None, None)
    assert record.outcome == "passed" and not record.composite


# --- P13: the route choice as a second signal, the coverage guard --------------------------------


def test_on_a_confident_intent_route_choice_is_a_composite_of_that_intent() -> None:
    # "Give me today's summary": the choice said day_summary at 0.86, its yes/no only 0.3.
    decision = RouteDecision(
        kind=RouteKind.INTENT,
        label="day_summary",
        intent="day_summary",
        confidence=0.86,
        assessment=Assessment.of(
            {"day_summary": 0.3}, action=0.02, complexity=0.1, requests=1
        ),
    )
    use_case, _, routes = _use_case(decision, FastPathMode.ON)

    routed = _run(use_case)

    assert routed.composite is not None and routed.composite.intents == ("day_summary",)
    assert routes.records[0].fast_path_intents == "day_summary"


def test_on_a_message_with_an_unplanned_request_runs_the_entry_profile() -> None:
    # Two requests, one detected intent: the composite would drop the other part.
    decision = RouteDecision(
        kind=RouteKind.INTENT,
        label="order_plan",
        intent="order_plan",
        confidence=0.6,
        assessment=Assessment.of(
            {"order_plan": 0.81, "day_summary": 0.2}, action=0.02, complexity=0.1, requests=2
        ),
    )
    use_case, _, routes = _use_case(decision, FastPathMode.ON)

    routed = _run(use_case)

    assert routed.request == _request(), "the entry profile, as the person"
    assert routed.composite is None and routed.intent is None, "never the one-intent route"
    record = routes.records[0]
    assert record.fast_path == "pass:uncovered" and record.outcome == "passed"
    assert record.assessment is not None and record.assessment.endswith(";requests=2")


def test_shadow_records_an_uncovered_message_but_runs_todays_route() -> None:
    decision = RouteDecision(
        kind=RouteKind.INTENT,
        label="order_plan",
        intent="order_plan",
        confidence=0.6,
        assessment=Assessment.of({"order_plan": 0.81}, action=0.02, complexity=0.1, requests=2),
    )
    use_case, _, routes = _use_case(decision, FastPathMode.SHADOW)

    routed = _run(use_case)

    assert routed.intent is not None and routed.intent.intent == "order_plan", "today's route"
    assert routes.records[0].fast_path == "pass:uncovered"


# --- P14: a confident route that covers the one detected intent is kept --------------------------

# One topic, said twice: the router's request count says two things (0.94), the yes/no values one
# intent, and the route choice names the profile that owns that intent's plan.
ONE_TOPIC_TWICE = Assessment.of(
    {"day_summary": 0.93, "order_plan": 0.04},
    action=0.06,
    complexity=0.41,
    requests=2,
    request_levels=(0.06, 0.94, 0.0, 0.0),
)


def _sure_route(
    profile: str = SALES, confidence: float = 1.0, assessment: Assessment = ONE_TOPIC_TWICE
) -> RouteDecision:
    return RouteDecision(
        kind=RouteKind.ROUTE,
        profile_id=profile,
        label=profile,
        confidence=confidence,
        assessment=assessment,
    )


def test_on_a_sure_specialist_route_covering_the_one_intent_is_kept() -> None:
    use_case, _, routes = _use_case(_sure_route(), FastPathMode.ON)

    routed = _run(use_case)

    assert routed.request.profile_id == SALES, "the router's own route, not the entry profile"
    assert routed.composite is None
    record = routes.records[0]
    assert record.fast_path == "defer:route_covers" and record.outcome == "routed"
    assert record.fast_path_intents == "day_summary"


def test_shadow_and_off_run_the_same_turn_for_a_covering_route() -> None:
    on, _, _ = _use_case(_sure_route(), FastPathMode.ON)
    shadow, _, shadow_routes = _use_case(_sure_route(), FastPathMode.SHADOW)
    off, _, off_routes = _use_case(_sure_route(), FastPathMode.OFF)

    expected = _run(off).request
    assert _run(shadow).request == expected == _run(on).request
    assert shadow_routes.records[0].fast_path == "defer:route_covers"
    assert off_routes.records[0].fast_path is None


def test_on_a_route_that_does_not_cover_the_intent_still_passes_uncovered() -> None:
    # STOCK owns order_plan, not day_summary: the second thing asked has no plan anywhere.
    use_case, _, routes = _use_case(_sure_route(profile=STOCK), FastPathMode.ON)

    routed = _run(use_case)

    assert routed.request == _request(), "the entry profile, as the person"
    assert routes.records[0].fast_path == "pass:uncovered"


def test_on_an_unsure_covering_route_still_passes_uncovered() -> None:
    use_case, _, routes = _use_case(_sure_route(confidence=0.85), FastPathMode.ON)

    routed = _run(use_case)

    assert routed.request == _request()
    assert routes.records[0].fast_path == "pass:uncovered"


def test_on_action_follow_up_and_complex_passes_never_keep_the_route() -> None:
    cases = {
        "pass:action_request": Assessment.of(
            {"day_summary": 0.93}, action=0.9, complexity=0.1, requests=1
        ),
        "pass:complex": Assessment.of(
            {"day_summary": 0.93}, action=0.02, complexity=0.9, requests=1
        ),
    }
    for code, assessment in cases.items():
        use_case, _, routes = _use_case(_sure_route(assessment=assessment), FastPathMode.ON)
        assert _run(use_case).request == _request(), code
        assert routes.records[0].fast_path == code
    follow = Assessment.of(
        {"day_summary": 0.93}, action=0.02, follow_up=0.9, complexity=0.1, requests=1
    )
    use_case, _, routes = _use_case(
        _sure_route(assessment=follow), FastPathMode.ON, history=["person: hi"]
    )
    assert _run(use_case).request == _request()
    assert routes.records[0].fast_path == "pass:depends_on_history"


def test_on_a_sure_intent_route_choice_of_the_one_topic_is_its_composite() -> None:
    # The live audit row of "is there data that does not add up?": route choice 1.0, count two.
    decision = RouteDecision(
        kind=RouteKind.INTENT,
        label="day_summary",
        intent="day_summary",
        confidence=1.0,
        assessment=ONE_TOPIC_TWICE,
    )
    use_case, _, routes = _use_case(decision, FastPathMode.ON)

    routed = _run(use_case)

    assert routed.composite is not None and routed.composite.intents == ("day_summary",)
    assert routes.records[0].fast_path == "composite"
    assert routes.records[0].assessment is not None
    assert routes.records[0].assessment.endswith(";requests=2;request_levels=0.06,0.94,0.00,0.00")
