"""C27 (D61): agent modes - `primary | peer | subagent | all`.

Subjects: domain/agent_mode.py (`AgentMode`), domain/profile.py (the `mode:` key),
adapters/driven/peers/hop_limit.py (`authorise_hop`'s callee mode), adapters/driven/peers/
catalogue.py, adapters/driving/workflow/turn_workflow.py (`_step_ask_peers`),
application/start_turn.py (a channel cannot start a non-entry agent) and the HTTP door.

What is defended: the default is `all`, so a profile that never says is unchanged; a `primary`
cannot be asked (the model reads a refused C8 envelope) and a `subagent` is not offered as a peer;
a channel cannot start a `subagent` (or a `peer`) agent; a peer's own turn and a background
follow-up are NOT channel starts and still run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_core.adapters.driven.peers.catalogue import build_peer_catalogue
from agent_core.adapters.driven.peers.hop_limit import HopRefusal, authorise_hop
from agent_core.adapters.driving.http.routes import create_app
from agent_core.application.start_turn import ModeRefusedError, StartTurn
from agent_core.domain.agent_mode import AgentMode
from agent_core.domain.peers import (
    AgentId,
    AgentRef,
    PeerPolicy,
    background_followup_turn_id,
)
from agent_core.domain.policy import RuleSet
from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.turn import (
    CallerIdentity,
    PeerOwner,
    SessionRef,
    TurnId,
    TurnRequest,
)
from tests.fakes import fanout
from tests.fakes import ports as fakes
from tests.unit.test_start_turn import (
    FINISHED,
    FakeContextEngine,
    FakeSkillRegistry,
    _request,
)

# --- the profile ---------------------------------------------------------------------------


def _profile(**extra: object) -> AgentProfile:
    return AgentProfile.from_mapping({"id": "p", "persona": "P", "model": "m", **extra})


def test_a_profile_that_never_says_is_mode_all() -> None:
    assert _profile().mode is AgentMode.ALL


@pytest.mark.parametrize("mode", ["primary", "peer", "subagent", "all"])
def test_each_mode_loads(mode: str) -> None:
    assert _profile(mode=mode).mode is AgentMode(mode)


@pytest.mark.parametrize("bad", ["Primary", "worker", "", None, 1, True])
def test_a_misspelt_mode_refuses_to_load(bad: object) -> None:
    with pytest.raises(ProfileValidationError, match="mode"):
        _profile(mode=bad)


def test_the_mode_is_part_of_the_content_hash() -> None:
    assert _profile(mode="primary").content_hash != _profile().content_hash


@pytest.mark.parametrize(
    ("mode", "startable", "askable", "spawnable"),
    [
        (AgentMode.PRIMARY, True, False, False),
        (AgentMode.PEER, False, True, False),
        (AgentMode.SUBAGENT, False, False, True),
        (AgentMode.ALL, True, True, True),
    ],
)
def test_what_each_mode_allows(
    mode: AgentMode, startable: bool, askable: bool, spawnable: bool
) -> None:
    assert (mode.startable_by_channel, mode.askable, mode.spawnable) == (
        startable,
        askable,
        spawnable,
    )


# --- the gate and the catalogue --------------------------------------------------------------


def _policy(*allows: str) -> PeerPolicy:
    return PeerPolicy(
        enabled=True, peers=tuple(AgentRef(AgentId(a), a) for a in allows), max_hops=3
    )


@pytest.mark.parametrize(
    ("mode", "allowed"),
    [
        (AgentMode.ALL, True),
        (AgentMode.PEER, True),
        (AgentMode.PRIMARY, False),
        (AgentMode.SUBAGENT, False),
    ],
)
def test_the_hop_gate_refuses_a_callee_that_cannot_be_asked(mode: AgentMode, allowed: bool) -> None:
    decision = authorise_hop(
        caller=AgentId("a"),
        caller_policy=_policy("b"),
        callee=AgentId("b"),
        callee_policy=_policy("a"),
        hop=0,
        callee_mode=mode,
    )

    assert decision.allowed is allowed
    if not allowed:
        assert decision.reason is HopRefusal.CALLEE_MODE_NOT_ASKABLE


def test_the_gate_defaults_to_mode_all() -> None:
    decision = authorise_hop(
        caller=AgentId("a"),
        caller_policy=_policy("b"),
        callee=AgentId("b"),
        callee_policy=_policy("a"),
        hop=0,
    )

    assert decision.allowed


def _world(callee_mode: AgentMode) -> Mapping[str, AgentProfile]:
    return {
        "asker": AgentProfile(id="asker", persona="a", model="m", peers=_policy("callee")),
        "callee": AgentProfile(
            id="callee", persona="c", model="m", peers=_policy("asker"), mode=callee_mode
        ),
    }


@pytest.mark.parametrize("mode", [AgentMode.PRIMARY, AgentMode.SUBAGENT])
def test_the_catalogue_hides_an_agent_that_cannot_be_asked(mode: AgentMode) -> None:
    world = _world(mode)

    assert build_peer_catalogue(world["asker"], world, permits=lambda _: True) == ()


@pytest.mark.parametrize("mode", [AgentMode.PEER, AgentMode.ALL])
def test_the_catalogue_lists_an_agent_that_can_be_asked(mode: AgentMode) -> None:
    world = _world(mode)

    listed = build_peer_catalogue(world["asker"], world, permits=lambda _: True)

    assert [entry.agent_id for entry in listed] == ["callee"]


# --- the workflow: asking a primary is a refused C8 envelope ------------------------------------


def test_asking_a_primary_is_refused_with_the_envelope_and_nothing_is_posted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, (fanout.ask("tc-1"),), [])
    fanout.callee_profile(mode=AgentMode.PRIMARY)

    asyncio.run(fanout.body()(fanout.request()))

    assert world.mailbox.asks == [], "a refused ask never leaves"
    text = str(dict(world.resume.calls[0])["tc-1"])
    assert 'status="refused"' in text
    assert "callee_mode_not_askable" in text, "the model is told why"


def test_asking_a_peer_mode_agent_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    world = fanout.install(monkeypatch, (fanout.ask("tc-1"),), [fanout.answer("corr-1", "hi")])
    fanout.callee_profile(mode=AgentMode.PEER)

    asyncio.run(fanout.body()(fanout.request()))

    assert len(world.mailbox.asks) == 1
    assert 'status="ok"' in str(dict(world.resume.calls[0])["tc-1"])


# --- starting a turn from a channel -------------------------------------------------------------


def _follow_up_check(known: tuple[str, ...]) -> Any:
    async def check(session: SessionRef, turn_id: TurnId) -> bool:
        return str(turn_id) in known

    return check


def _start_turn(
    mode: AgentMode, caller: CallerIdentity, *, follow_ups: tuple[str, ...] = ()
) -> tuple[StartTurn, fakes.FakeAgentRunner]:
    profile = AgentProfile(id="delivery_optimizer", persona="p", model="m", mode=mode)
    runner = fakes.FakeAgentRunner(FINISHED)
    use_case = StartTurn(
        runner=runner,
        tools=fakes.FakeToolProvider(toolset=object(), tool_names=()),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(caller, ())),
        store=fakes.FakeConversationStore(),
        audit=fakes.FakeAuditSink(),
        context=FakeContextEngine(),  # type: ignore[arg-type]
        skills=FakeSkillRegistry(),  # type: ignore[arg-type]
        profiles={profile.id: profile},
        is_follow_up=_follow_up_check(follow_ups),
    )
    return use_case, runner


@pytest.mark.parametrize("mode", [AgentMode.SUBAGENT, AgentMode.PEER])
def test_a_channel_cannot_start_a_subagent_or_peer_only_agent(
    mode: AgentMode, session: SessionRef, caller: CallerIdentity
) -> None:
    use_case, runner = _start_turn(mode, caller)

    with pytest.raises(ModeRefusedError, match="delivery_optimizer"):
        asyncio.run(
            use_case.execute(TurnId("t-1"), _request(session, caller, "delivery_optimizer"))
        )

    assert runner.run_calls == [], "the model never ran"


@pytest.mark.parametrize("mode", [AgentMode.PRIMARY, AgentMode.ALL])
def test_a_channel_can_start_a_primary_or_all_agent(
    mode: AgentMode, session: SessionRef, caller: CallerIdentity
) -> None:
    use_case, runner = _start_turn(mode, caller)

    asyncio.run(use_case.execute(TurnId("t-1"), _request(session, caller, "delivery_optimizer")))

    assert len(runner.run_calls) == 1


def test_a_peers_own_turn_and_a_follow_up_run_whatever_the_mode(
    session: SessionRef, caller: CallerIdentity
) -> None:
    follow_up = background_followup_turn_id("corr-1")
    use_case, runner = _start_turn(AgentMode.SUBAGENT, caller, follow_ups=(follow_up,))
    plain = _request(session, caller, "delivery_optimizer")
    owned = TurnRequest(
        session=plain.session,
        caller=plain.caller,
        profile_id=plain.profile_id,
        input=plain.input,
        owner=PeerOwner("root", "asker", "orch", "call"),
    )

    asyncio.run(use_case.execute(TurnId("t-1"), owned))
    asyncio.run(use_case.execute(TurnId(follow_up), plain))

    assert len(runner.run_calls) == 2


def test_with_no_follow_up_check_bound_a_turn_nobody_owns_is_a_channel_start(
    session: SessionRef, caller: CallerIdentity
) -> None:
    use_case, _ = _start_turn(AgentMode.SUBAGENT, caller)
    use_case._is_follow_up = None  # noqa: SLF001

    with pytest.raises(ModeRefusedError):
        asyncio.run(
            use_case.execute(TurnId("t-1"), _request(session, caller, "delivery_optimizer"))
        )


# --- the HTTP door ---------------------------------------------------------------------------


class _Handle:
    turn_id = TurnId("t-1")


def _client(startable: bool) -> tuple[TestClient, list[TurnRequest]]:
    started: list[TurnRequest] = []

    async def start(request: TurnRequest) -> Any:
        started.append(request)
        return _Handle()

    app = create_app(start_turn=start, channel_startable=lambda profile_id: startable)
    return TestClient(app), started


_HEADERS = {"X-Subject-Id": "u", "X-Tenant-Id": "t", "X-Channel": "http"}
_BODY = {"session_id": "s-1", "profile_id": "p", "text": "hi"}


def test_the_door_refuses_a_profile_a_channel_may_not_start_before_any_turn() -> None:
    client, started = _client(False)

    response = client.post("/turns", json=_BODY, headers=_HEADERS)

    assert response.status_code == 422
    assert "p" in response.json()["detail"]
    assert started == []


def test_the_door_lets_a_startable_profile_through() -> None:
    client, started = _client(True)

    assert client.post("/turns", json=_BODY, headers=_HEADERS).status_code == 202
    assert len(started) == 1
