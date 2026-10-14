"""Intent plans: declared by addons, resolved by the router seam (D97, S3/S6 layer B).

Subject: domain/intents.py (the plan, its parser, argument resolution, templates), the `intent`
branch of domain/routing.py `resolve_route`, `RouteTurn` handing the intent on, the profile's
`routing.intents` opt-in, and `provides.intents` registration.

THE RULES UNDER TEST. A plan names its owning profile, up to eight steps (a tool and arguments
that are constants, the router's arguments with an optional default, or a context value) and a
reply mode (`compose`, or `template` with `{step_id}` placeholders). Registration refuses a plan
whose tool no toolset exports, is not replay-safe (a write) or delegates/waits. An intent lands
only when the starting profile opted in, a plan exists and its owner is an allowed, startable
target; otherwise it falls back with the reason, exactly like a route.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agent_core.adapters.driven.addons_fs.errors import AddonRegistrationError, ManifestError
from agent_core.adapters.driven.addons_fs.loader import load_addons
from agent_core.adapters.driven.addons_fs.registration import (
    INTERRUPTING_TOOLS,
    register_addons,
)
from agent_core.adapters.driven.tools import evidence, peer_background, peers, subagents
from agent_core.application.route_turn import RouteTurn
from agent_core.composition import TOOL_PACKAGES
from agent_core.domain.agent_mode import AgentMode
from agent_core.domain.intents import (
    IntentCall,
    IntentPlanError,
    PlanArgumentError,
    PlanStep,
    ReplyMode,
    SourceKind,
    parse_intent_plans,
    render_template,
    resolve_arguments,
)
from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.routing import (
    FALLBACK_INTENT_NOT_SUPPORTED,
    FALLBACK_INTENT_UNKNOWN,
    FALLBACK_TARGET_NOT_ALLOWED,
    FALLBACK_TARGET_NOT_STARTABLE,
    RouteDecision,
    RouteKind,
    RouteRecord,
    RouteRequest,
    RoutingPolicy,
    resolve_route,
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

PLANS = """\
intents:
  quote:
    profile: billing
    steps:
      - tool: pricing_quote
        arguments:
          zone: north                       # a constant
          weight: {argument: weight, default: 1}
          day: {context: today}
          raw: {constant: {argument: x}}    # a constant that looks like a source
      - id: second
        tool: routing_estimate
    reply: template
    template: "Quote: {pricing_quote} / {second}"
  orders:
    profile: billing
    steps:
      - tool: orders_lookup
        arguments: {order_id: {argument: order_id}}
"""


def _plans(text: str = PLANS) -> Any:
    import yaml

    return parse_intent_plans(yaml.safe_load(text), source="plans.yaml")


# --- the plan ------------------------------------------------------------------------------------


def test_a_plan_names_its_profile_steps_arguments_and_reply() -> None:
    plans = _plans()

    quote = plans["quote"]
    assert (quote.profile_id, quote.reply, quote.tools) == (
        "billing", ReplyMode.TEMPLATE, ("pricing_quote", "routing_estimate"),
    )
    assert [step.id for step in quote.steps] == ["pricing_quote", "second"]
    sources = dict(quote.steps[0].arguments)
    assert sources["zone"].kind is SourceKind.CONSTANT and sources["zone"].value == "north"
    assert (sources["weight"].kind, sources["weight"].name, sources["weight"].value) == (
        SourceKind.ARGUMENT, "weight", 1,
    )
    assert sources["weight"].has_default
    assert (sources["day"].kind, sources["day"].name) == (SourceKind.CONTEXT, "today")
    assert sources["raw"].kind is SourceKind.CONSTANT and sources["raw"].value == {"argument": "x"}
    assert plans["orders"].reply is ReplyMode.COMPOSE and plans["orders"].template is None


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("  x: {profile: p, steps: [{tool: t}], extra: 1}", "unknown keys"),
        ("  x: {profile: p, steps: []}", "non-empty"),
        ("  x: {profile: p, steps: [" + ", ".join(f"{{tool: t{i}}}" for i in range(9)) + "]}",
         "at most 8"),
        ("  x: {profile: p, steps: [{tool: t}, {tool: t}]}", "share an id"),
        ("  x: {profile: p, steps: [{tool: t}], reply: template}", "needs a non-empty"),
        ("  x: {profile: p, steps: [{tool: t}], reply: template, template: '{nope}'}",
         "unknown steps"),
        ("  x: {profile: p, steps: [{tool: t}], reply: template, template: '{t.attr}'}",
         "bare step id"),
        ("  x: {profile: p, steps: [{tool: t}], template: '{t}'}", "only for"),
        ("  x: {profile: p, steps: [{tool: t}], reply: shout}", "compose"),
        ("  x: {profile: p, steps: [{tool: t, arguments: {a: {context: secrets}}}]}",
         "is not one of"),
        ("  x: {profile: p, steps: [{tool: t, arguments: {a: {context: today, default: 1}}}]}",
         "default"),
        ("  x: {profile: p, steps: [{tool: t, arguments: {a: {argument: b, context: today}}}]}",
         "more than one"),
        ("  x: {steps: [{tool: t}]}", "profile"),
        ("  x: {profile: p, steps: [{tool: 'not a tool'}]}", "tool"),
    ],
)
def test_a_plan_that_cannot_work_is_refused_naming_the_intent(body: str, match: str) -> None:
    with pytest.raises(IntentPlanError, match=match) as raised:
        _plans("intents:\n" + body + "\n")
    assert "'x'" in str(raised.value)


def test_a_plans_document_is_one_intents_mapping() -> None:
    for document in ("[]", "intents: {}", "intents: {a: {profile: p, steps: [{tool: t}]}}\nx: 1"):
        with pytest.raises(IntentPlanError):
            _plans(document)


def test_arguments_resolve_from_constants_the_router_defaults_and_context() -> None:
    quote = _plans()["quote"]

    call = resolve_arguments(quote.steps[0], {"weight": 7}, {"today": "2026-10-14"})
    defaulted = resolve_arguments(quote.steps[0], {}, {"today": "2026-10-14"})

    assert (call.step_id, call.tool) == ("pricing_quote", "pricing_quote")
    assert call.arguments == {
        "zone": "north", "weight": 7, "day": "2026-10-14", "raw": {"argument": "x"},
    }
    assert defaulted.arguments["weight"] == 1


def test_an_argument_the_router_did_not_extract_is_refused_naming_it_not_its_value() -> None:
    orders = _plans()["orders"]
    with pytest.raises(PlanArgumentError, match="order_id"):
        resolve_arguments(orders.steps[0], {"other": "secret-value"}, {})


def test_a_template_puts_each_steps_result_in_its_place() -> None:
    assert render_template("A {a}; B {b} {{literal}}", {"a": "1", "b": "two"}) == (
        "A 1; B two {literal}"
    )


def test_an_intent_call_and_a_step_hold_plain_values() -> None:
    call = IntentCall("quote", {"weight": 3})
    assert type(call.arguments) is dict
    assert PlanStep(id="s", tool="t").arguments == ()


# --- where an intent lands -----------------------------------------------------------------------

OWNERS = {"quote": "billing", "chat": "front", "ops": "ops", "helper_plan": "helper"}


def _land(policy: RoutingPolicy, intent: str, plans: dict[str, str] | None = OWNERS) -> Any:
    return resolve_route(
        RouteDecision(kind=RouteKind.INTENT, intent=intent),
        policy,
        requested="front",
        entry_points={"front", "billing", "ops"},
        plans=plans,
    )


def test_an_intent_lands_on_its_plans_owner_only_when_the_profile_opted_in() -> None:
    opted = RoutingPolicy(router="r", targets=("billing", "helper"), intents=True)
    silent = RoutingPolicy(router="r", targets=("billing",))

    assert _land(opted, "quote") == ("billing", None)
    assert _land(opted, "chat") == ("front", None), "a plan of the requested profile itself"
    assert _land(silent, "quote") == ("front", FALLBACK_INTENT_NOT_SUPPORTED)
    assert _land(opted, "quote", None) == ("front", FALLBACK_INTENT_NOT_SUPPORTED)
    assert _land(opted, "unplanned") == ("front", FALLBACK_INTENT_UNKNOWN)
    assert _land(opted, "ops") == ("front", FALLBACK_TARGET_NOT_ALLOWED)
    assert _land(opted, "helper_plan") == ("front", FALLBACK_TARGET_NOT_STARTABLE)


def test_a_route_still_lands_exactly_as_before() -> None:
    policy = RoutingPolicy(router="r", targets=("billing",), intents=True)
    decision = RouteDecision(kind=RouteKind.ROUTE, profile_id="billing")
    assert resolve_route(
        decision, policy, requested="front", entry_points={"front", "billing"}, plans=OWNERS
    ) == ("billing", None)
    assert resolve_route(
        RouteDecision(kind=RouteKind.ROUTE, profile_id="ops"),
        policy,
        requested="front",
        entry_points={"front", "ops"},
    ) == ("front", FALLBACK_TARGET_NOT_ALLOWED)


def test_a_profile_opts_into_intents_in_its_routing_block() -> None:
    def load(routing: dict[str, Any]) -> AgentProfile:
        return AgentProfile.from_mapping(
            {"id": "front", "persona": "p", "model": "m", "routing": routing}
        )

    base = {"router": "r", "targets": ["billing"]}
    assert load(base).routing == RoutingPolicy(router="r", targets=("billing",), intents=False)
    assert load({**base, "intents": True}).routing.intents is True  # type: ignore[union-attr]
    with pytest.raises(ProfileValidationError, match="intents"):
        load({**base, "intents": "yes"})


class _Router:
    def __init__(self, decision: RouteDecision) -> None:
        self.decision = decision

    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        return self.decision


class _Audit:
    def __init__(self) -> None:
        self.records: list[RouteRecord] = []

    async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
        self.records.append(record)


def _route_turn(decision: RouteDecision, *, intents: bool = True) -> tuple[RouteTurn, _Audit]:
    front = AgentProfile.from_mapping(
        {
            "id": "front",
            "persona": "p",
            "model": "m",
            "routing": {"router": "r", "targets": ["billing"], "intents": intents},
        }
    )
    profiles = {
        "front": front,
        "billing": AgentProfile(id="billing", persona="b", model="m"),
        "helper": AgentProfile(id="helper", persona="h", model="m", mode=AgentMode.PEER),
    }
    audit = _Audit()
    return (
        RouteTurn(routers=_Router(decision), audit=audit, profiles=profiles, plans=_plans()),
        audit,
    )


def _request() -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t")),
        caller=CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t")),
        profile_id="front",
        input=UserInput(text="how much to ship 3 kg"),
    )


def test_a_landed_intent_runs_as_the_plans_owner_and_carries_the_routers_arguments() -> None:
    decision = RouteDecision(
        kind=RouteKind.INTENT, intent="quote", arguments={"weight": 3}, label="quote"
    )
    use_case, audit = _route_turn(decision)

    routed = asyncio.run(use_case.execute(TurnId("turn-1"), _request()))

    assert routed.request.profile_id == "billing"
    assert routed.intent == IntentCall("quote", {"weight": 3})
    assert routed.record.fallback_reason is None and routed.record.intent == "quote"
    assert audit.records == [routed.record]


@pytest.mark.parametrize(
    ("decision", "intents", "reason"),
    [
        (RouteDecision(kind=RouteKind.INTENT, intent="quote"), False,
         FALLBACK_INTENT_NOT_SUPPORTED),
        (RouteDecision(kind=RouteKind.INTENT, intent="nope"), True, FALLBACK_INTENT_UNKNOWN),
        (RouteDecision(kind=RouteKind.PASS), True, None),
        (RouteDecision(kind=RouteKind.ROUTE, profile_id="billing"), True, None),
    ],
)
def test_no_intent_is_handed_on_unless_one_landed(
    decision: RouteDecision, intents: bool, reason: str | None
) -> None:
    use_case, _ = _route_turn(decision, intents=intents)

    routed = asyncio.run(use_case.execute(TurnId("turn-1"), _request()))

    assert routed.intent is None
    assert routed.record.fallback_reason == reason


# --- registration --------------------------------------------------------------------------------


def _addon(root: Path, name: str, plans: str, *, toolset: str | None = None) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    provides = "  intents: plans.yaml\n"
    if toolset is not None:
        provides += f"  toolsets: [{toolset}]\n"
        (directory / "toolsets").mkdir()
        (directory / "toolsets" / f"{toolset}.py").write_text(
            dedent(
                """\
                from pydantic_ai.tools import Tool
                from pydantic_ai.toolsets import FunctionToolset


                def stock_level(sku: str) -> int:
                    return 3


                def build_toolset() -> FunctionToolset:
                    return FunctionToolset(
                        [Tool(stock_level, metadata={"replay": "safe"})]
                    )
                """
            ),
            encoding="utf-8",
        )
    (directory / "addon.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\ncore: '>=0'\nprovides:\n{provides}", encoding="utf-8"
    )
    (directory / "plans.yaml").write_text(plans, encoding="utf-8")


def _register(root: Path, enabled: list[str]) -> Any:
    return register_addons(
        load_addons(enabled=enabled, addons_root=root, core_version="0.0.0"),
        builtin_toolsets=TOOL_PACKAGES,
        builtin_fragments_dir=None,
        reserved_channels=("http",),
    )


def _one(tool: str) -> str:
    return f"intents:\n  x:\n    profile: p\n    steps: [{{tool: {tool}}}]\n"


def test_an_addon_declares_plans_over_read_tools_of_core_and_of_its_own(tmp_path: Path) -> None:
    _addon(tmp_path, "shop", _one("stock_level"), toolset="shop_tools")
    _addon(tmp_path, "quotes", _one("pricing_quote").replace("x:", "y:"))

    contributions = _register(tmp_path, ["shop", "quotes"])

    assert set(contributions.intents) == {"x", "y"}
    assert contributions.intents["x"].tools == ("stock_level",)
    assert contributions.toolset_tools["shop_tools"] == frozenset({"stock_level"})
    assert contributions.toolset_tools["delivery"] >= {"pricing_quote", "pricing_apply"}


@pytest.mark.parametrize(
    ("tool", "match"),
    [
        ("pricing_apply", "not declared replay-safe"),
        ("freeze_account", "not declared replay-safe"),
        ("ask_peer", "delegates or waits"),
        ("spawn_subagent", "delegates or waits"),
        ("peer_stop", "delegates or waits"),
        ("no_such_tool", "no enabled toolset exports"),
    ],
)
def test_a_plan_naming_a_write_an_interrupting_or_an_unknown_tool_is_refused_at_registration(
    tmp_path: Path, tool: str, match: str
) -> None:
    _addon(tmp_path, "shop", _one(tool))
    with pytest.raises(AddonRegistrationError, match=match):
        _register(tmp_path, ["shop"])


def test_two_addons_planning_the_same_intent_stop_startup(tmp_path: Path) -> None:
    _addon(tmp_path, "one", _one("pricing_quote"))
    _addon(tmp_path, "two", _one("orders_lookup"))
    with pytest.raises(AddonRegistrationError, match="already planned by addon 'one'"):
        _register(tmp_path, ["one", "two"])


def test_a_malformed_plans_file_stops_startup_naming_the_addon(tmp_path: Path) -> None:
    _addon(tmp_path, "shop", "intents:\n  x: {profile: p, steps: []}\n")
    with pytest.raises(AddonRegistrationError, match="shop"):
        _register(tmp_path, ["shop"])


def test_a_declared_plans_file_must_exist(tmp_path: Path) -> None:
    _addon(tmp_path, "shop", _one("pricing_quote"))
    (tmp_path / "shop" / "plans.yaml").unlink()
    with pytest.raises(ManifestError, match="provides.intents"):
        _register(tmp_path, ["shop"])


def test_the_interrupting_tools_are_exactly_cores_delegating_tools() -> None:
    builders = (
        peers.build_toolset,
        subagents.build_toolset,
        evidence.build_toolset,
        peer_background.build_toolset,
    )
    assert frozenset(tool for build in builders for tool in build().tools) == INTERRUPTING_TOOLS
