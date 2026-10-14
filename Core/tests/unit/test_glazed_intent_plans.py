"""The glazed layer-B intent plans: glazed_vertical/routing/intent_plans.yaml (D97).

THE RULES UNDER TEST. The vertical declares one plan for each of the ten intents its System One
routes name (the L9 table); every plan registers (each tool is exported, replay-safe and not a
delegating tool), runs as the specialist that owns that data, which is a loaded entry point
granted the tool; every constant argument is valid against the tool's schema; and nothing turns the
plans on by itself (no glazed profile opts in).
"""

from __future__ import annotations

import jsonschema
import pytest
import yaml

from agent_core.composition import refuse_unplannable_intents
from agent_core.domain.intents import SourceKind
from tests.unit import glazed_vertical_support as glazed

ROUTES = glazed.ADDON / "routing" / "system_one_routes.yaml"
L9 = {
    "day_summary": "glazed_present",
    "active_issues": "glazed_present",
    "order_plan": "glazed_supply",
    "stockout_risk": "glazed_supply",
    "expiring_items": "glazed_present",
    "upcoming_deliveries": "glazed_supply",
    "staffing": "glazed_strategist",
    "promo_list": "glazed_strategist",
    "data_integrity": "glazed_auditor",
    "transfer_options": "glazed_liaison",
}


def test_the_vertical_plans_exactly_the_ten_intents_its_routes_name() -> None:
    routes = yaml.safe_load(ROUTES.read_text(encoding="utf-8"))["routes"]
    targets = [r["target"] for r in routes]
    routed = {t.removeprefix("intent:") for t in targets if t.startswith("intent:")}

    plans = glazed.contributions().intents

    assert set(plans) == routed == set(L9)
    assert {intent: plan.profile_id for intent, plan in plans.items()} == L9


def test_every_plan_runs_as_a_startable_specialist_granted_its_tools() -> None:
    contributions = glazed.contributions()
    profiles = {glazed.profile(role).id: glazed.profile(role) for role in glazed.ROLES}

    refuse_unplannable_intents(profiles, contributions.intents, contributions.toolset_tools)


@pytest.mark.parametrize("intent", sorted(L9))
def test_every_constant_argument_is_valid_for_its_tool(intent: str) -> None:
    contributions = glazed.contributions()
    plan = contributions.intents[intent]
    toolset = contributions.toolsets[plan.profile_id]()
    for step in plan.steps:
        assert all(source.kind is SourceKind.CONSTANT for _, source in step.arguments), (
            "the System One router extracts no arguments: a glazed plan uses constants only"
        )
        arguments = {name: source.value for name, source in step.arguments}
        schema = toolset.tools[step.tool].function_schema.json_schema
        jsonschema.validate(arguments, schema)


def test_no_glazed_profile_turns_the_plans_on() -> None:
    for path in (glazed.ADDON / "profiles").glob("*.yaml"):
        assert "routing" not in yaml.safe_load(path.read_text(encoding="utf-8")), path.name
