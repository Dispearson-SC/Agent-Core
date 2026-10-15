"""The glazed deployment's Phase 5 speedups (P5): what glazed_vertical turns on, and only there.

Subject: addons/glazed_vertical/profiles/glazed_orchestrator.yaml (its `routing:` and `peers:`).

THE RULES UNDER TEST. The orchestrator, and no other glazed profile, opts into turn routing with the
`system_one` router: the seven specialists are its route targets (layer A) and the ten intent plans
may answer (layer B, `intents: true`). The block is inert until `AGENT_CORE_TURN_ROUTING` is on (the
startup checks run only then, and the off path loads the same profiles as before); with routing on
and the router addon enabled, the shipped profiles pass both startup checks. The orchestrator
collects every peer answer of a step (`peers.fan_out: all_settled`), and two independent asks it
issues in ONE step (S9: the persona asks every independent specialist in the same step) are both
dispatched in that round and answered in one resume. No specialist answers typed data: their
replies are prose the orchestrator reads, and a typed profile cannot own a plan.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
import yaml

from agent_core.adapters.driven.addons_fs.loader import load_addons_from_environment
from agent_core.adapters.driven.addons_fs.registration import register_addons
from agent_core.adapters.driven.policy_fs.loader import load_policy_rules
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.composition import (
    TOOL_PACKAGES,
    refuse_unplannable_intents,
    refuse_unroutable_profiles,
)
from agent_core.domain.peers import FanOutPolicy
from tests.fakes import fanout
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


@pytest.mark.parametrize("role", glazed.ROLES)
def test_every_glazed_role_thinks_with_low_reasoning_effort(role: str) -> None:
    """L24: with no effort a specialist's answer spent 1.7k reasoning tokens (35.6 s live) and
    `low` 0.5k (14.0 s) for the same facts; the orchestrator's final reply 19.1 s -> 8.8 s. Set
    once in the base, inherited by every role, no output bound (a cut reply is not a reply)."""
    settings = glazed.profile(role).model_settings

    assert settings is not None
    assert settings.reasoning_effort == "low"
    assert settings.max_tokens is None


def test_the_orchestrator_tells_its_peers_the_conversation_verbatim_not_a_summary() -> None:
    """L24: `summary` made a model call before EVERY ask (6-9 s live between the ask and the
    peer's start, once 36 s) and it was cut at its 500-token bound; `full` reads the newest
    messages within the same 4000-character cap with no model call."""
    from agent_core.domain.peers import PeerVisibility

    peers = glazed.profile("orchestrator").peers
    assert peers.visibility is PeerVisibility.FULL
    assert peers.max_context_chars == 4000


def test_the_orchestrator_collects_every_peer_answer_of_a_step() -> None:
    """`all_settled` is also Core's default, so the loaded value alone would pass with the line
    deleted: the profile must SAY it, so a change of Core's default never changes glazed."""
    peers = glazed.profile("orchestrator").peers
    assert peers.enabled and peers.fan_out is FanOutPolicy.ALL_SETTLED
    declared = yaml.safe_load(
        (glazed.ADDON / "profiles" / "glazed_orchestrator.yaml").read_text(encoding="utf-8")
    )
    assert declared["peers"]["fan_out"] == "all_settled"


def test_two_independent_asks_of_one_step_are_dispatched_together_and_answered_in_one_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S9, scripted: the glazed orchestrator's model step defers two ask_peer calls to two
    specialists. Both leave in that round (two mailbox asks before any answer is read), the turn
    waits for both, and it resumes ONCE with both answers - the hang the old one-per-step rule
    worked around does not happen."""
    world = fanout.install(
        monkeypatch,
        (
            fanout.ask("tc-1", target="glazed_present"),
            fanout.ask("tc-2", target="glazed_supply"),
        ),
        [fanout.answer("corr-1", "ventas de hoy"), fanout.answer("corr-2", "cobertura")],
    )
    seat = turn_workflow._dependencies.peers  # noqa: SLF001 - the test seam
    assert seat is not None
    seat.profiles.clear()
    seat.profiles.update({f"glazed_{role}": glazed.profile(role) for role in glazed.ROLES})
    request = dataclasses.replace(fanout.request(), profile_id="glazed_orchestrator")

    outcome = asyncio.run(fanout.body()(request))

    assert outcome.result is not None
    assert [target for target, _ in world.mailbox.asks] == ["glazed_present", "glazed_supply"]
    assert len(world.resume.calls) == 1, "one resume for the whole step"
    resumed = dict(world.resume.calls[0])
    assert set(resumed) == {"tc-1", "tc-2"}
    assert 'status="ok"' in str(resumed["tc-1"]) and "ventas de hoy" in str(resumed["tc-1"])
    assert 'status="ok"' in str(resumed["tc-2"]) and "cobertura" in str(resumed["tc-2"])
    assert world.mailbox.cancelled == {}


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
