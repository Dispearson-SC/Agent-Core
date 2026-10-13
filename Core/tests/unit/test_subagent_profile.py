"""C28 (D62): the `subagents:` block of a profile, the delegation kind and the subagent tool name.

Subjects: domain/subagents.py (`SubagentPolicy`), domain/agent_mode.py (`DelegationKind`),
domain/profile.py (the `subagents:` key), domain/policy.py (`resource_of` for `spawn_subagent`),
domain/peer_result.py (the `kind` attribute of the envelope).
"""

from __future__ import annotations

import pytest

from agent_core.domain.agent_mode import DelegationKind
from agent_core.domain.peer_result import PeerResult, PeerStatus
from agent_core.domain.peers import AgentId
from agent_core.domain.policy import resource_of
from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.subagents import DEFAULT_SUBAGENT_DEPTH, SubagentPolicy


def _profile(**subagents: object) -> AgentProfile:
    return AgentProfile.from_mapping(
        {"id": "p", "persona": "P", "model": "m", "subagents": subagents}
    )


def test_subagents_are_off_by_default_and_nobody_is_allowed() -> None:
    profile = AgentProfile.from_mapping({"id": "p", "persona": "P", "model": "m"})

    assert profile.subagents == SubagentPolicy()
    assert not profile.subagents.enabled and profile.subagents.agents == ()
    assert profile.subagents.depth == DEFAULT_SUBAGENT_DEPTH == 1
    assert not profile.subagents.may_spawn(AgentId("worker"))


def test_the_block_loads_and_the_allowlist_is_membership() -> None:
    profile = _profile(enabled=True, agents=["researcher", "writer"], depth=2)

    policy = profile.subagents
    assert policy.enabled and policy.depth == 2
    assert policy.agents == (AgentId("researcher"), AgentId("writer"))
    assert policy.may_spawn(AgentId("writer")) and not policy.may_spawn(AgentId("other"))


def test_an_empty_allowlist_means_nobody_not_everybody() -> None:
    assert not _profile(enabled=True).subagents.may_spawn(AgentId("anyone"))


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("enabled", "yes"),
        ("enabled", 1),
        ("depth", 0),
        ("depth", -1),
        ("depth", 1.5),
        ("depth", True),
        ("agents", "researcher"),
        ("agents", [""]),
        ("agents", [1]),
        ("agents", ["a", "a"]),
        ("surprise", True),
    ],
)
def test_a_malformed_block_refuses_to_load(key: str, bad: object) -> None:
    with pytest.raises(ProfileValidationError, match="subagents"):
        _profile(**{key: bad})


def test_the_block_is_part_of_the_content_hash() -> None:
    base = _profile().content_hash

    assert _profile(enabled=True).content_hash != base
    assert _profile(agents=["a"]).content_hash != base
    assert _profile(depth=2).content_hash != base


def test_a_policy_rule_resource_for_spawn_is_the_agent_argument() -> None:
    assert resource_of("spawn_subagent", {"agent": "researcher", "task": "t"}) == "researcher"
    assert resource_of("spawn_subagent", {"task": "t"}) is None
    assert resource_of("spawn_subagent", {"agent": 3}) is None
    assert resource_of("ask_peer", {"target": "billing"}) == "billing", "unchanged"


def test_the_envelope_names_a_subagent_and_leaves_a_peer_untouched() -> None:
    peer = PeerResult(peer="billing", status=PeerStatus.OK, text="x").render()
    sub = PeerResult(
        peer="researcher", status=PeerStatus.OK, text="x", kind=DelegationKind.SUBAGENT
    ).render()

    assert "kind=" not in peer, "a peer's envelope is byte-for-byte what it was"
    assert 'kind="subagent"' in sub and sub.startswith('<peer id="researcher"')


# --- who the peer worker drains a queue for ---------------------------------------------------


def test_the_worker_polls_a_subagent_that_a_spawner_may_spawn_and_one_declared_as_such() -> None:
    from agent_core.adapters.driving.peers.worker import answerable_targets
    from agent_core.domain.agent_mode import AgentMode

    def load(profile_id: str, **fields: object) -> AgentProfile:
        return AgentProfile.from_mapping({"id": profile_id, "persona": "P", "model": "m", **fields})

    profiles = {
        "boss": load(
            "boss",
            subagents={"enabled": True, "agents": ["listed"]},
        ),
        "listed": load("listed"),  # mode all, no peers: reached only because the boss lists it
        "declared": load("declared", mode="subagent"),
        "unrelated": load("unrelated"),  # never opted in: not polled
        "asked": load("asked", peers={"enabled": True}),
        "disabled-boss": load("disabled-boss", subagents={"agents": ["unrelated"]}),
    }
    assert profiles["declared"].mode is AgentMode.SUBAGENT

    polled = answerable_targets(profiles)

    assert polled == (AgentId("asked"), AgentId("declared"), AgentId("listed"))


def test_the_console_renders_a_spawn_as_a_spawn() -> None:
    from agent_core.adapters.driving.cli.console import _raw_delegation_ask

    assert _raw_delegation_ask({"agent": "researcher", "task": "find it"}) == (
        "spawned researcher: find it"
    )
    assert _raw_delegation_ask({"target": "billing", "question": "q"}) == "asked billing: q"
    assert _raw_delegation_ask({"agent": "researcher"}) == "spawned researcher (no task recorded)"
