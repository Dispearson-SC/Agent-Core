"""C17 (D42): one topology source, validated at startup.

Subject: domain/topology.py (`topology_violations`, `validate_topology`), `peers.topology` in
domain/profile.py, and the startup wiring in composition.py (`build_container`).

A profile's allowlist (`peers.peers`), its hop limit and the policy rules were three places that
had to agree about who may ask whom, and nothing checked them: a specialist quietly allowing
another specialist, or an edge the policy denied for everybody, only showed up as refused asks.
ONE profile (the hub or root) now declares `peers.topology: star | tree | mesh` and the process
refuses to start when the profiles reachable from it, their hop limits or the policy files say
otherwise. Nothing declared, nothing checked.

An allowlist entry is MUTUAL by the two-sided gate (`hop_limit.authorise_hop`): A may ask B only
if A lists B AND B lists A. So a spoke lists its hub, and a spoke that lists another spoke is
the drift.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from agent_core import composition
from agent_core.domain.peers import Topology
from agent_core.domain.policy import Effect, PolicyRule
from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.topology import TopologyDriftError, topology_violations, validate_topology
from agent_core.domain.turn import TenantId


def _profile(
    profile_id: str,
    *,
    asks: tuple[str, ...] = (),
    topology: str | None = None,
    max_hops: int = 3,
    enabled: bool = True,
) -> AgentProfile:
    peers: dict[str, Any] = {
        "enabled": enabled,
        "max_hops": max_hops,
        "peers": [{"agent_id": a, "display_name": a.title()} for a in asks],
    }
    if topology is not None:
        peers["topology"] = topology
    return AgentProfile.from_mapping(
        {"id": profile_id, "persona": profile_id, "model": "m", "peers": peers}
    )


def _world(*profiles: AgentProfile) -> dict[str, AgentProfile]:
    return {profile.id: profile for profile in profiles}


def _star() -> dict[str, AgentProfile]:
    return _world(
        _profile("hub", asks=("billing", "fraud"), topology="star"),
        _profile("billing", asks=("hub",)),
        _profile("fraud", asks=("hub",)),
    )


def _allow(tool: str = "ask_peer", **kwargs: Any) -> PolicyRule:
    return PolicyRule(rule_id="allow", tool_pattern=tool, effect=Effect.ALLOW, reason="r", **kwargs)


# --- star -------------------------------------------------------------------------------------


def test_a_consistent_star_passes() -> None:
    validate_topology(_star())


def test_a_star_with_a_specialist_allowing_another_specialist_fails_with_a_clear_message() -> None:
    world = _star()
    world["billing"] = _profile("billing", asks=("hub", "fraud"))

    with pytest.raises(TopologyDriftError) as caught:
        validate_topology(world)

    message = str(caught.value)
    assert "billing" in message and "fraud" in message and "star" in message
    assert "hub" in message, "the message names the hub the spokes may talk to"


def test_a_spoke_that_does_not_list_its_hub_is_a_dead_edge() -> None:
    world = _star()
    world["fraud"] = _profile("fraud")  # hub lists fraud; fraud lists nobody

    (violation,) = topology_violations(world)

    assert "fraud" in violation and "hub" in violation
    assert "callee_does_not_allow_caller" in violation


def test_nothing_declared_nothing_checked() -> None:
    world = _world(
        _profile("a", asks=("b", "c")),  # a mess: no topology declared
        _profile("b", asks=("c",)),
        _profile("c"),
    )

    assert topology_violations(world) == ()


def test_two_profiles_declaring_a_topology_is_drift() -> None:
    world = _star()
    world["billing"] = _profile("billing", asks=("hub",), topology="star")

    (violation,) = topology_violations(world)

    assert "once" in violation and "hub" in violation and "billing" in violation


def test_a_listed_peer_with_no_profile_is_drift() -> None:
    world = _star()
    world["hub"] = _profile("hub", asks=("billing", "fraud", "ghost"), topology="star")

    violations = topology_violations(world)

    assert any("ghost" in v and "no profile" in v for v in violations)


def test_peers_switched_off_on_either_side_of_an_edge_is_drift() -> None:
    world = _star()
    world["fraud"] = _profile("fraud", asks=("hub",), enabled=False)

    violations = topology_violations(world)

    assert any("fraud" in v and "peers.enabled" in v for v in violations)


def test_a_declaring_profile_with_peers_disabled_is_drift() -> None:
    world = _star()
    world["hub"] = _profile("hub", asks=("billing", "fraud"), topology="star", enabled=False)

    assert any("hub" in v and "enabled" in v for v in topology_violations(world))


# --- tree -------------------------------------------------------------------------------------


def _tree() -> dict[str, AgentProfile]:
    return _world(
        _profile("root", asks=("a", "b"), topology="tree"),
        _profile("a", asks=("root", "a1")),
        _profile("b", asks=("root",)),
        _profile("a1", asks=("a",)),
    )


def test_a_consistent_tree_passes() -> None:
    validate_topology(_tree())


def test_a_node_with_two_parents_breaks_the_tree() -> None:
    world = _tree()
    world["b"] = _profile("b", asks=("root", "a1"))  # a1 now has parents a and b
    world["a1"] = _profile("a1", asks=("a", "b"))

    violations = topology_violations(world)

    assert any("a1" in v and "two parents" in v for v in violations)


def test_a_cycle_breaks_the_tree() -> None:
    world = _tree()
    world["a1"] = _profile("a1", asks=("a", "root"))  # a1 -> root closes a loop

    assert any("root" in v and "tree" in v for v in topology_violations(world))


def test_a_tree_edge_made_deeper_than_the_hop_limit_is_drift() -> None:
    world = _tree()
    world["a"] = _profile("a", asks=("root", "a1"), max_hops=1)  # a asks a1 at depth 1

    violations = topology_violations(world)

    assert any("a" in v and "a1" in v and "max_hops" in v for v in violations)


# --- mesh -------------------------------------------------------------------------------------


def test_a_mutual_mesh_passes_and_a_one_sided_entry_fails() -> None:
    mesh = _world(
        _profile("a", asks=("b", "c"), topology="mesh"),
        _profile("b", asks=("a", "c")),
        _profile("c", asks=("a", "b")),
    )
    validate_topology(mesh)

    mesh["c"] = _profile("c", asks=("a",))  # b lists c, c does not list b

    (violation,) = topology_violations(mesh)
    assert "b" in violation and "c" in violation and "callee_does_not_allow_caller" in violation


# --- hop limits -------------------------------------------------------------------------------


def test_a_hop_limit_that_refuses_the_first_hop_is_drift() -> None:
    world = _star()
    world["billing"] = _profile("billing", asks=("hub",), max_hops=0)

    assert any("billing" in v and "max_hops" in v for v in topology_violations(world))


# --- policy rules -----------------------------------------------------------------------------


def test_an_edge_the_policy_allows_passes() -> None:
    assert topology_violations(_star(), [_allow()]) == ()
    assert topology_violations(_star(), [_allow(resource_pattern="*")]) == ()
    assert topology_violations(_star(), [_allow(subject_roles=frozenset({"operator"}))]) == ()


def test_an_edge_no_rule_allows_is_drift() -> None:
    violations = topology_violations(_star(), [_allow(tool="knowledge_search")])

    assert any("billing" in v and "no policy rule allows" in v for v in violations)


def test_an_edge_a_rule_denies_for_everyone_is_drift() -> None:
    deny = PolicyRule(
        rule_id="no-fraud",
        tool_pattern="ask_peer",
        effect=Effect.DENY,
        reason="never",
        resource_pattern="fraud",
    )

    violations = topology_violations(_star(), [_allow(), deny])

    assert any("fraud" in v and "no-fraud" in v for v in violations)
    assert not any("billing" in v for v in violations)


def test_a_deny_for_some_roles_or_one_tenant_is_not_drift() -> None:
    narrow = PolicyRule(
        rule_id="not-for-guests",
        tool_pattern="ask_peer",
        effect=Effect.DENY,
        reason="x",
        resource_pattern="fraud",
        subject_roles=frozenset({"guest"}),
    )
    tenant = dataclasses.replace(
        narrow, rule_id="tenant", subject_roles=frozenset(), tenant_id=TenantId("t")
    )

    assert topology_violations(_star(), [_allow(), narrow, tenant]) == ()


def test_without_policy_files_the_policy_is_not_checked() -> None:
    assert topology_violations(_star(), None) == ()


# --- the profile surface ------------------------------------------------------------------------


def test_a_profile_declares_no_topology_by_default_and_parses_the_three_shapes() -> None:
    assert _profile("p").peers.topology is None
    for shape in Topology:
        assert _profile("p", topology=shape.value).peers.topology is shape


def test_an_unknown_topology_refuses_to_load() -> None:
    with pytest.raises(ProfileValidationError, match="topology"):
        _profile("p", topology="ring")


def test_the_topology_is_part_of_the_content_hash() -> None:
    assert _profile("p").content_hash != _profile("p", topology="star").content_hash


# --- startup ------------------------------------------------------------------------------------


def _write_profiles(directory: Path, world: dict[str, AgentProfile]) -> None:
    import yaml  # type: ignore[import-untyped]

    directory.mkdir(parents=True, exist_ok=True)
    for profile in world.values():
        peers: dict[str, Any] = {
            "enabled": profile.peers.enabled,
            "max_hops": profile.peers.max_hops,
            "peers": [
                {"agent_id": str(ref.agent_id), "display_name": ref.display_name}
                for ref in profile.peers.peers
            ],
        }
        if profile.peers.topology is not None:
            peers["topology"] = profile.peers.topology.value
        document = {"id": profile.id, "persona": profile.id, "model": "m", "peers": peers}
        (directory / f"{profile.id}.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def _settings(tmp_path: Path) -> Any:
    return dataclasses.replace(
        composition.Settings.from_env(),
        profiles_dir=tmp_path / "profiles",
        policy_dir=tmp_path / "policy",
    )


def test_the_process_refuses_to_start_on_a_drifting_topology(tmp_path: Path) -> None:
    world = _star()
    world["billing"] = _profile("billing", asks=("hub", "fraud"))
    _write_profiles(tmp_path / "profiles", world)

    with pytest.raises(TopologyDriftError, match="billing"):
        composition.build_container(_settings(tmp_path))


def test_the_process_starts_on_a_consistent_topology_and_checks_the_policy_files(
    tmp_path: Path,
) -> None:
    _write_profiles(tmp_path / "profiles", _star())
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "rules.yaml").write_text(
        "rules:\n  - rule_id: other\n    tool_pattern: knowledge_search\n    effect: allow\n"
        "    reason: x\n",
        encoding="utf-8",
    )

    with pytest.raises(TopologyDriftError, match="no policy rule allows"):
        composition.build_container(_settings(tmp_path))

    (policy / "rules.yaml").write_text(
        "rules:\n  - rule_id: peers\n    tool_pattern: ask_peer\n    effect: allow\n"
        "    reason: x\n",
        encoding="utf-8",
    )
    composition.build_container(_settings(tmp_path))


def test_the_shipped_profiles_and_policy_declare_nothing_and_still_start() -> None:
    composition.build_container()
