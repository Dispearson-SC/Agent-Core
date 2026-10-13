"""C1r R1: the spawned profile must consent to its spawner.

Subjects: domain/subagents.py (`SubagentPolicy.accept_from`), adapters/driven/peers/hop_limit.py
(`authorise_spawn`), adapters/driven/peers/catalogue.py (`build_subagent_catalogue`),
domain/profile.py (the `subagents.accept_from` key).

A peer ask is checked on BOTH sides (the asker may ask, the answerer accepts being asked). A spawn
had only the spawner's side and the callee's `mode`, so any spawner that listed a worker could run
it whatever the worker's own profile said. `accept_from` is the worker's half: empty means nobody,
and it applies whether or not the worker itself may spawn (`enabled`).
"""

from __future__ import annotations

import pytest

from agent_core.adapters.driven.peers.catalogue import build_subagent_catalogue
from agent_core.adapters.driven.peers.hop_limit import SpawnRefusal, authorise_spawn
from agent_core.domain.agent_mode import AgentMode
from agent_core.domain.peers import AgentId
from agent_core.domain.profile import AgentProfile
from agent_core.domain.subagents import SubagentPolicy

BOSS, WORKER, OTHER = AgentId("boss"), AgentId("worker"), AgentId("other-boss")


def _gate(callee: SubagentPolicy) -> object:
    return authorise_spawn(
        caller=BOSS,
        caller_policy=SubagentPolicy(enabled=True, agents=(WORKER,)),
        callee=WORKER,
        callee_mode=AgentMode.SUBAGENT,
        hop=0,
        depth=0,
        callee_subagents=callee,
    )


def test_a_worker_that_names_nobody_accepts_nobody() -> None:
    assert not SubagentPolicy().accepts(BOSS)
    assert not SubagentPolicy(enabled=True, agents=(BOSS,)).accepts(BOSS)


def test_a_worker_accepts_exactly_the_spawners_it_lists() -> None:
    policy = SubagentPolicy(accept_from=(BOSS,))

    assert policy.accepts(BOSS) and not policy.accepts(OTHER)


def test_the_gate_refuses_a_spawner_the_worker_does_not_list() -> None:
    decision = _gate(SubagentPolicy(accept_from=(OTHER,)))

    assert not decision.allowed  # type: ignore[attr-defined]
    assert decision.reason is SpawnRefusal.CALLEE_DOES_NOT_ACCEPT_SPAWNER  # type: ignore[attr-defined]


def test_the_gate_allows_a_spawner_the_worker_lists() -> None:
    assert _gate(SubagentPolicy(accept_from=(BOSS,))).allowed  # type: ignore[attr-defined]


def test_the_spawners_own_refusal_is_reported_before_the_workers() -> None:
    decision = authorise_spawn(
        caller=BOSS,
        caller_policy=SubagentPolicy(enabled=True, agents=()),
        callee=WORKER,
        callee_mode=AgentMode.SUBAGENT,
        hop=0,
        depth=0,
        callee_subagents=SubagentPolicy(),
    )

    assert decision.reason is SpawnRefusal.CALLER_DOES_NOT_ALLOW_SUBAGENT


def test_the_key_loads_strictly_and_is_part_of_the_content_hash() -> None:
    def load(**block: object) -> AgentProfile:
        return AgentProfile.from_mapping(
            {"id": "w", "persona": "P", "model": "m", "subagents": block}
        )

    loaded = load(accept_from=["boss"])

    assert loaded.subagents.accepts(BOSS) and not loaded.subagents.accepts(OTHER)
    assert loaded.content_hash != load().content_hash
    for bad in ("boss", [""], [1], ["boss", "boss"]):
        with pytest.raises(Exception, match="accept_from"):
            load(accept_from=bad)


def test_the_menu_hides_a_worker_that_does_not_accept_the_spawner() -> None:
    def profile(pid: str, **fields: object) -> AgentProfile:
        return AgentProfile(id=pid, persona=pid, model="m", **fields)  # type: ignore[arg-type]

    boss = profile("boss", subagents=SubagentPolicy(enabled=True, agents=(WORKER, AgentId("shy"))))
    world = {
        "boss": boss,
        "worker": profile(
            "worker", mode=AgentMode.SUBAGENT, subagents=SubagentPolicy(accept_from=(BOSS,))
        ),
        "shy": profile("shy", mode=AgentMode.SUBAGENT),
    }

    listed = build_subagent_catalogue(boss, world, permits=lambda _: True)

    assert [e.agent_id for e in listed] == ["worker"]
