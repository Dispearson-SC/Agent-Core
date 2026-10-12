"""C20 (D50): `peer_list`, `peer_status`, `peer_message` and `peer_stop`.

Subjects: adapters/driven/tools/peer_background.py (the four tools), the runner handing them the
conversation (`TurnContext.background`, `BackgroundOwner`), composition granting them to a profile
that opted in. The seat is a fake here; its rules are `test_background_agents.py`'s.

What is defended:
  * the model is offered the tools only when the profile opted in AND a seat is wired;
  * each tool reports in plain text, in our voice, and never carries a peer's words (an answer
    reaches the model only through the follow-up turn, inside the untrusted boundary);
  * the seat is handed the conversation of the TURN (never a value the model chose) and the tool
    call id as the idempotency key of a message.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core import composition
from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.tools.background import (
    BackgroundOwner,
    MessageMode,
    MessageResult,
    StopResult,
)
from agent_core.adapters.driven.tools.peer_background import PEER_LIST_LIMIT, build_toolset
from agent_core.domain.peers import AgentId, AgentRef, BackgroundAsk, BackgroundState, PeerPolicy
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

TOOLS = ("peer_list", "peer_status", "peer_message", "peer_stop")
SESSION = SessionRef(session_id=SessionId("case-1"), tenant_id=TenantId("t"))
CALLER = CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t"))
AT = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)


def _ask(handle: str = "h-1", state: BackgroundState = BackgroundState.RUNNING) -> BackgroundAsk:
    return BackgroundAsk(
        handle=handle,
        target="billing",
        state=state,
        question="check every invoice of 2025",
        asked_at=AT,
        session="peer~case-1~k",
    )


class _Seat:
    def __init__(self) -> None:
        self.asks: tuple[BackgroundAsk, ...] = (_ask(),)
        self.owners: list[BackgroundOwner] = []
        self.limits: list[int] = []
        self.total: int | None = None  # the store's count when it is more than `asks`
        self.stopped: list[str] = []
        self.messages: list[tuple[str, str, MessageMode, str]] = []
        self.stop_result = StopResult.STOPPED
        self.message_result = MessageResult(handle="h-2")

    async def list(self, owner: BackgroundOwner, *, limit: int) -> tuple[BackgroundAsk, ...]:
        self.owners.append(owner)
        self.limits.append(limit)
        return self.asks[:limit]

    async def count(self, owner: BackgroundOwner) -> int:
        self.owners.append(owner)
        return len(self.asks) if self.total is None else self.total

    async def status(self, owner: BackgroundOwner, handle: str) -> BackgroundAsk | None:
        self.owners.append(owner)
        return next((a for a in self.asks if a.handle == handle), None)

    async def stop(self, owner: BackgroundOwner, handle: str) -> StopResult:
        self.owners.append(owner)
        self.stopped.append(handle)
        return self.stop_result

    async def message(
        self,
        owner: BackgroundOwner,
        handle: str,
        text: str,
        mode: MessageMode,
        *,
        call_id: str,
    ) -> MessageResult:
        self.owners.append(owner)
        self.messages.append((handle, text, mode, call_id))
        return self.message_result


def _profile(*, allow: bool = True) -> AgentProfile:
    return AgentProfile(
        id="asker",
        persona="p",
        model="m",
        peers=PeerPolicy(
            enabled=True,
            peers=(AgentRef(agent_id=AgentId("billing"), display_name="billing"),),
            allow_background=allow,
        ),
    )


def _run(
    calls: list[tuple[str, dict[str, Any]]],
    *,
    seat: _Seat | None,
    allow: bool = True,
) -> tuple[list[str], list[str]]:
    """Run one turn whose model calls `calls` in turn, then answers. Returns (tool returns, the
    names of the tools the model was offered on its first request)."""
    offered: list[str] = []
    steps = iter(calls)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not offered:
            offered.extend(t.name for t in info.function_tools)
        step = next(steps, None)
        if step is None:
            return ModelResponse(parts=[TextPart("done")])
        name, args = step
        return ModelResponse(
            parts=[ToolCallPart(tool_name=name, args=args, tool_call_id=f"call-{name}")]
        )

    rules = tuple(
        PolicyRule(rule_id=f"r-{n}", tool_pattern=n, effect=Effect.ALLOW, reason="ok")
        for n in TOOLS
    )
    runner = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"m": "m"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(CALLER, rules)),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(build_toolset(), TOOLS),
        model_factory=lambda model_id, base_url: FunctionModel(model),
        background_agents=seat,
    )
    request = TurnRequest(
        session=SESSION,
        caller=CALLER,
        profile_id="asker",
        input=UserInput(text="hello"),
    )
    captured: list[str] = []

    outcome = asyncio.run(runner.run(TurnId("turn-9"), request, _profile(allow=allow), None))
    assert outcome.result is not None
    for message in outcome.messages:
        for part in getattr(message, "parts", ()):
            if isinstance(part, ToolReturnPart):
                captured.append(str(part.content))
    return captured, offered


# --- availability -----------------------------------------------------------------------------


def test_the_tools_are_offered_to_a_profile_that_opted_in_with_a_seat() -> None:
    _, offered = _run([], seat=_Seat())

    assert set(TOOLS) <= set(offered)


def test_the_tools_are_hidden_without_a_seat_or_without_the_opt_in() -> None:
    assert not set(TOOLS) & set(_run([], seat=None)[1])
    assert not set(TOOLS) & set(_run([], seat=_Seat(), allow=False)[1])


# --- list and status --------------------------------------------------------------------------


def test_list_reports_each_background_agent_with_its_handle_and_state() -> None:
    seat = _Seat()
    seat.asks = (_ask("h-1"), _ask("h-0", BackgroundState.ANSWERED))

    (returned,), _ = _run([("peer_list", {})], seat=seat)

    assert "h-1" in returned and "running" in returned and "billing" in returned
    assert "h-0" in returned and "answered" in returned
    assert "check every invoice of 2025" in returned


def test_list_says_how_many_older_asks_it_left_out_from_the_stores_count() -> None:
    """C1o: the 'older' figure is the store's count minus what was listed, not a guess from a
    window that was itself cut."""
    seat = _Seat()
    seat.asks = tuple(_ask(f"h-{n}") for n in range(PEER_LIST_LIMIT + 5))
    seat.total = PEER_LIST_LIMIT + 75

    (returned,), _ = _run([("peer_list", {})], seat=seat)

    assert seat.limits == [PEER_LIST_LIMIT], "the tool asks for exactly what it will show"
    assert returned.count("- handle") == PEER_LIST_LIMIT
    assert "and 75 older ones" in returned


def test_list_of_an_empty_conversation_says_so() -> None:
    seat = _Seat()
    seat.asks = ()

    (returned,), _ = _run([("peer_list", {})], seat=seat)

    assert "no background" in returned.lower()


def test_status_of_a_handle_reports_it_and_of_an_unknown_one_says_so() -> None:
    (found,), _ = _run([("peer_status", {"handle": "h-1"})], seat=_Seat())
    (missing,), _ = _run([("peer_status", {"handle": "nope"})], seat=_Seat())

    assert "h-1" in found and "running" in found
    assert "no background agent" in missing.lower() and "nope" in missing


def test_the_seat_is_handed_the_conversation_of_the_turn_not_a_value_the_model_chose() -> None:
    seat = _Seat()

    _run([("peer_list", {})], seat=seat)

    (owner, *_) = seat.owners
    assert owner.session == SESSION and owner.profile_id == "asker"
    assert owner.caller == CALLER and owner.turn_id == "turn-9"


# --- stop -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (StopResult.STOPPED, "stopped"),
        (StopResult.NOT_RUNNING, "already finished"),
        (StopResult.UNKNOWN, "no background agent"),
    ],
)
def test_stop_says_what_it_did(result: StopResult, expected: str) -> None:
    seat = _Seat()
    seat.stop_result = result

    (returned,), _ = _run([("peer_stop", {"handle": "h-1"})], seat=seat)

    assert expected in returned.lower() and seat.stopped == ["h-1"]


# --- message ----------------------------------------------------------------------------------


def test_a_message_is_sent_with_its_mode_and_the_tool_call_as_its_key() -> None:
    seat = _Seat()

    (returned,), _ = _run(
        [("peer_message", {"handle": "h-1", "text": "use March", "mode": "steer"})], seat=seat
    )

    assert seat.messages == [("h-1", "use March", MessageMode.STEER, "call-peer_message")]
    assert "h-2" in returned and "billing" in returned


def test_the_default_mode_is_follow_up() -> None:
    seat = _Seat()

    _run([("peer_message", {"handle": "h-1", "text": "and also"})], seat=seat)

    assert seat.messages[0][2] is MessageMode.FOLLOW_UP


def test_a_refused_message_gives_the_reason() -> None:
    seat = _Seat()
    seat.message_result = MessageResult(refusal="that background agent was stopped")

    (returned,), _ = _run([("peer_message", {"handle": "h-1", "text": "x"})], seat=seat)

    assert "not sent" in returned.lower() and "was stopped" in returned


def test_the_tools_never_repeat_the_peers_words() -> None:
    """An answer reaches the model only inside the follow-up turn's untrusted boundary."""
    seat = _Seat()

    returns, _ = _run(
        [("peer_list", {}), ("peer_status", {"handle": "h-1"}), ("peer_stop", {"handle": "h-1"})],
        seat=seat,
    )

    assert "untrusted" not in " ".join(returns).lower()


# --- composition ------------------------------------------------------------------------------


def test_composition_grants_the_tools_to_a_profile_that_opted_in() -> None:
    plain = _profile(allow=False)
    opted = _profile(allow=True)

    assert (
        composition.PEER_BACKGROUND_TOOLSET
        not in composition._grant_peer_mechanism(  # noqa: SLF001
            plain
        ).toolsets
    )
    assert (
        composition.PEER_BACKGROUND_TOOLSET
        in composition._grant_peer_mechanism(  # noqa: SLF001
            opted
        ).toolsets
    )
    assert composition.PEER_BACKGROUND_TOOLSET in composition.TOOL_PACKAGES
