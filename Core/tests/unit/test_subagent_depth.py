"""C29 (D63): depth and ownership limits for subagents.

Subjects: adapters/driven/peers/hop_limit.py (`authorise_spawn`), adapters/driving/workflow/
turn_workflow.py (`_step_ask_peers`: the depth and hop a child travels with),
adapters/driven/agent_pydantic/runner.py (the menu of a turn at its limit).

The depth of a chain is the number of subagent links down to a turn. It is carried on the ask row
and on `PeerOwner` (set from the row by the worker), each hop adding one for a spawn and passing it
on unchanged for a peer ask, so no walk of the rows is needed at spawn time. `subagents.depth`
(default 1) bounds it; the STRICTER of the spawner's and the spawned profile's number wins (like
`max_hops`). A spawn is also a hop: it consumes `peers.max_hops` like an ask, so subagents and
peers share one bound on how far work can be delegated.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from agent_core.adapters.driven.peers.hop_limit import SpawnRefusal, authorise_spawn
from agent_core.domain.agent_mode import AgentMode, DelegationKind
from agent_core.domain.peers import AgentId, PeerPolicy
from agent_core.domain.subagents import SubagentPolicy
from agent_core.domain.turn import PeerOwner, TurnOutcome, TurnRequest
from tests.fakes import fanout

SUB = DelegationKind.SUBAGENT
WORKER = AgentId("worker")
SPAWNER = AgentId("boss")


def _policy(depth: int = 1, *agents: AgentId) -> SubagentPolicy:
    return SubagentPolicy(enabled=True, agents=agents or (WORKER,), depth=depth)


def _gate(
    *,
    depth: int = 0,
    hop: int = 0,
    caller_depth: int = 1,
    callee_depth: int = 1,
    caller_hops: int = 2,
    callee_hops: int = 2,
) -> Any:
    return authorise_spawn(
        caller=SPAWNER,
        caller_policy=_policy(caller_depth),
        callee=WORKER,
        callee_mode=AgentMode.SUBAGENT,
        hop=hop,
        depth=depth,
        callee_subagents=SubagentPolicy(depth=callee_depth, accept_from=(SPAWNER,)),
        caller_peers=PeerPolicy(max_hops=caller_hops),
        callee_peers=PeerPolicy(max_hops=callee_hops),
    )


# --- the gate ---------------------------------------------------------------------------------


def test_a_turn_a_person_started_may_spawn_one_level_by_default() -> None:
    decision = _gate(depth=0)

    assert decision.allowed and (decision.next_depth, decision.next_hop) == (1, 1)


def test_a_subagent_at_depth_one_may_not_spawn_under_the_default_limit() -> None:
    decision = _gate(depth=1)

    assert not decision.allowed and decision.reason is SpawnRefusal.SUBAGENT_DEPTH_REACHED


def test_a_deeper_limit_allows_exactly_that_many_levels() -> None:
    assert _gate(depth=1, caller_depth=2, callee_depth=2).next_depth == 2
    assert (
        _gate(depth=2, caller_depth=2, callee_depth=2).reason is SpawnRefusal.SUBAGENT_DEPTH_REACHED
    )


def test_the_stricter_of_the_two_depth_limits_wins_in_both_directions() -> None:
    assert (
        _gate(depth=1, caller_depth=5, callee_depth=1).reason is SpawnRefusal.SUBAGENT_DEPTH_REACHED
    )
    assert (
        _gate(depth=1, caller_depth=1, callee_depth=5).reason is SpawnRefusal.SUBAGENT_DEPTH_REACHED
    )


def test_a_spawn_is_a_hop_and_the_stricter_hop_limit_wins() -> None:
    assert _gate(hop=1, caller_hops=2, callee_hops=2).next_hop == 2
    assert _gate(hop=1, caller_hops=1, callee_hops=9).reason is SpawnRefusal.HOP_LIMIT_REACHED
    assert _gate(hop=1, caller_hops=9, callee_hops=1).reason is SpawnRefusal.HOP_LIMIT_REACHED


def test_a_depth_refusal_is_reported_before_a_hop_refusal() -> None:
    both = _gate(depth=1, hop=5)

    assert both.reason is SpawnRefusal.SUBAGENT_DEPTH_REACHED


# --- the workflow -----------------------------------------------------------------------------


def _as_subagent(depth: int, hop: int = 0) -> TurnRequest:
    return dataclasses.replace(
        fanout.request(),
        hop=hop,
        owner=PeerOwner("root", "parent", "boss", "corr", kind=SUB, subagent_depth=depth),
    )


def _run(request: TurnRequest) -> TurnOutcome:
    return asyncio.run(fanout.body()(request))


def _world(monkeypatch: pytest.MonkeyPatch, *pending: Any, **subagents: Any) -> fanout.World:
    world = fanout.install(monkeypatch, tuple(pending), [fanout.answer("corr-1", "ok")])
    fanout.asker_subagents(**subagents)
    fanout.callee_profile(mode=AgentMode.SUBAGENT)
    fanout.asker_policy(max_hops=5)
    seat = fanout.turn_workflow._dependencies.peers  # noqa: SLF001
    assert seat is not None
    seat.profiles[fanout.PEER] = dataclasses.replace(
        seat.profiles[fanout.PEER],
        peers=dataclasses.replace(seat.profiles[fanout.PEER].peers, max_hops=5),
    )
    return world


def test_a_subagent_that_tries_to_spawn_at_depth_one_gets_a_clear_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch, fanout.spawn("tc-1"))

    _run(_as_subagent(depth=1))

    assert world.mailbox.asks == [], "nothing was started"
    text = str(dict(world.resume.calls[0])["tc-1"])
    assert 'status="refused"' in text and 'kind="subagent"' in text
    assert "subagent_depth_reached" in text


def test_the_same_spawn_is_allowed_where_both_profiles_allow_the_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch, fanout.spawn("tc-1"), depth=2)
    seat = fanout.turn_workflow._dependencies.peers  # noqa: SLF001
    assert seat is not None
    seat.profiles[fanout.PEER] = dataclasses.replace(
        seat.profiles[fanout.PEER],
        subagents=SubagentPolicy(depth=2, accept_from=(AgentId(fanout.ASKER),)),
    )

    _run(_as_subagent(depth=1))

    assert world.mailbox.depths == [2] and world.mailbox.kinds == [SUB]


def test_a_spawn_travels_one_hop_deeper_and_the_hop_limit_still_bounds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch, fanout.spawn("tc-1"))
    _run(dataclasses.replace(fanout.request(), hop=2))
    assert world.mailbox.hops == [3]

    fresh = _world(monkeypatch, fanout.spawn("tc-1"))
    fanout.asker_policy(max_hops=2)
    _run(dataclasses.replace(fanout.request(), hop=2))
    assert fresh.mailbox.asks == []
    assert "hop_limit_reached" in str(dict(fresh.resume.calls[0])["tc-1"])


def test_a_peer_ask_made_from_inside_a_subagent_carries_the_chains_depth_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So a peer the subagent asks, and anything THAT spawns, still counts the link above it."""
    world = fanout.install(monkeypatch, (fanout.ask("tc-1"),), [fanout.answer("corr-1", "ok")])
    fanout.asker_policy(max_hops=5)

    _run(_as_subagent(depth=1))

    assert world.mailbox.kinds == [DelegationKind.PEER] and world.mailbox.depths == [1]


def test_a_peer_ask_from_a_turn_with_no_subagent_above_it_stays_at_depth_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, (fanout.ask("tc-1"),), [fanout.answer("corr-1", "ok")])

    _run(fanout.request())

    assert world.mailbox.depths == [0]
