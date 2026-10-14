"""The glazed deployment's Phase 5 speedups (P5): what glazed_vertical turns on, and only there.

Subject: addons/glazed_vertical/profiles/glazed_orchestrator.yaml (its `routing:` and `peers:`).

THE RULES UNDER TEST. The orchestrator, and no other glazed profile, opts into turn routing with the
`system_one` router: the seven specialists are its route targets (layer A) and the ten intent plans
may answer (layer B, `intents: true`). The block is inert until `AGENT_CORE_TURN_ROUTING` is on (the
startup checks run only then, and the off path loads the same profiles as before); with routing on
and the router addon enabled, the shipped profiles pass both startup checks. The orchestrator
collects every peer answer of a step (`peers.fan_out: all_settled`). No specialist answers typed
data: their replies are prose the orchestrator reads, and a typed profile cannot own a plan.
"""

from __future__ import annotations

import yaml

from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.addons_fs.registration import register_addons
from agent_core.adapters.driven.policy_fs.loader import load_policy_rules
from agent_core.composition import (
    TOOL_PACKAGES,
    refuse_unplannable_intents,
    refuse_unroutable_profiles,
)
from agent_core.domain.peers import FanOutPolicy
from tests.unit import glazed_vertical_support as glazed

SPECIALISTS = tuple(f"glazed_{role}" for role in glazed.ROLES if role != "orchestrator")


def test_the_orchestrator_routes_with_system_one_to_the_seven_specialists_and_the_plans() -> None:
    routing = glazed.profile("orchestrator").routing

    assert routing is not None
    assert routing.router == "system_one"
    assert set(routing.targets) == set(SPECIALISTS) and len(routing.targets) == 7
    assert routing.intents is True


def test_no_other_glazed_profile_routes() -> None:
    for role in glazed.ROLES[1:]:
        assert glazed.profile(role).routing is None, role


def test_the_orchestrator_collects_every_peer_answer_of_a_step() -> None:
    """`all_settled` is also Core's default, so the loaded value alone would pass with the line
    deleted: the profile must SAY it, so a change of Core's default never changes glazed."""
    peers = glazed.profile("orchestrator").peers
    assert peers.enabled and peers.fan_out is FanOutPolicy.ALL_SETTLED
    declared = yaml.safe_load(
        (glazed.ADDON / "profiles" / "glazed_orchestrator.yaml").read_text(encoding="utf-8")
    )
    assert declared["peers"]["fan_out"] == "all_settled"


def test_no_specialist_answers_typed_data() -> None:
    for role in glazed.ROLES:
        assert glazed.profile(role).answer_schema is None, role


def test_with_routing_on_and_the_router_enabled_the_shipped_profiles_pass_startup() -> None:
    environment = {
        **glazed.ENVIRONMENT,
        "AGENT_CORE_ADDONS": glazed.ENABLED + ",system_one_router",
        "AGENT_CORE_ADDON__SYSTEM_ONE_ROUTER__ROUTES_FILE": (
            "glazed_vertical/routing/system_one_routes.yaml"
        ),
        "AGENT_CORE_ADDON__SYSTEM_ONE_ROUTER__API_KEY_ENV": "JEV_API_KEY",
    }
    contributions = register_addons(
        load_addons_from_environment(environment),
        builtin_toolsets=TOOL_PACKAGES,
        builtin_fragments_dir=glazed.CORE / "profiles" / "fragments",
        reserved_channels=("http", "cli"),
        builtin_rule_ids=tuple(r.rule_id for r in load_policy_rules(glazed.CORE / "policy")),
        manages_policy_files=True,
    )
    profiles = glazed.served(glazed.CORE / "profiles")

    refuse_unroutable_profiles(profiles, frozenset(contributions.routers))
    refuse_unplannable_intents(profiles, contributions.intents, contributions.toolset_tools)
    assert "system_one" in contributions.routers
