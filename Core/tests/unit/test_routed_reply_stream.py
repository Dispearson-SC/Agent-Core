"""P11 (S8, L20): a ROUTED turn streams its reply to the person, and a compose that is not a reply
takes back the text it streamed.

Subjects: adapters/driven/agent_pydantic/runner.py (`_reply_stream`, `compose_reply`) and
adapters/driven/agent_pydantic/reply_stream.py (`ReplyStreamHandler.withdraw`).

THE RULES UNDER TEST
    1. A routed turn (layer A route or layer B intent plan) runs under the delegated identity: the
       peer identity with `on_behalf_of` = the person. It is still the PERSON's turn (nobody else
       owns it: `request.owner is None`), so its reply streams to the turn's feed exactly like the
       person's own turn - both `run` and the intent plan's `compose_reply`.
    2. A real peer turn (an ask_peer child, `request.owner` set) never streams, delegated or not.
    3. A compose cut at its token bound, or one that fails while it streams, falls back to the
       normal path (`ComposeIncompleteError` / the error). The text it already streamed must not
       stay on the feed contradicting the fallback reply: the handler publishes ONE empty chunk of
       a new response, which by the feed's rule (a chunk of a later run or response starts the
       text over) leaves the turn's `reply_text` empty. A complete compose publishes no such chunk.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.agent_pydantic.intent_plan import ComposeIncompleteError
from agent_core.domain.events import (
    EventKind,
    ProgressEvent,
    StoredEvent,
    fold_snapshot,
    progress_event_id,
)
from agent_core.domain.intents import PlanToolResult
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    PeerOwner,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
    delegated_identity,
)
from tests.fakes import ports as fakes

PERSON = CallerIdentity(subject_id="manager-1", channel="shop", tenant_id=TenantId("t"))
SESSION = SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t"))
PROFILE = AgentProfile.from_mapping({"id": "p", "persona": "P", "model": "minimax/MiniMax-M3"})
RESULTS = (PlanToolResult("stock_levels", "stock_levels", content='{"units": 2}'),)
TURN = TurnId("turn-1")


class _Events:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    async def publish(self, event: ProgressEvent) -> None:
        self.events.append(event)

    @property
    def deltas(self) -> list[ProgressEvent]:
        return [e for e in self.events if e.kind is EventKind.DELTA]

    def reply_text(self) -> str | None:
        """The turn's `reply_text` in a snapshot folded from what was published."""
        started = ProgressEvent(
            event_id=progress_event_id(str(TURN), EventKind.STARTED),
            kind=EventKind.STARTED,
            tenant_id="t",
            session_id="s",
            turn_id=str(TURN),
            agent="p",
            at=datetime(2026, 10, 15, tzinfo=UTC),
        )
        stored = tuple(StoredEvent(i + 1, e) for i, e in enumerate([started, *self.events]))
        (turn,) = fold_snapshot(str(TURN), stored).turns
        return turn.reply_text


def _runner(model: FunctionModel, events: _Events) -> Any:
    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(PERSON, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        model_factory=lambda model_id, base_url: model,
        reply_events=events,
    )


def _request(*, routed: bool = True, peer: bool = False) -> TurnRequest:
    return TurnRequest(
        session=SESSION,
        caller=delegated_identity(PERSON) if routed else PERSON,
        profile_id="p",
        input=UserInput(text="¿cómo vamos hoy?"),
        hop=1 if routed else 0,
        owner=PeerOwner("root", "asker", "orch", "call-1") if peer else None,
    )


def _words(*parts: str) -> FunctionModel:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        for part in parts:
            yield part

    def whole(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("".join(parts))])

    return FunctionModel(whole, stream_function=stream)


class _CutModel(FunctionModel):
    """Streams its words and then reports the response as cut at the token bound."""

    @asynccontextmanager
    async def request_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async with super().request_stream(*args, **kwargs) as response:
            response.finish_reason = "length"
            yield response


def _cut(*parts: str) -> FunctionModel:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        for part in parts:
            yield part

    return _CutModel(stream_function=stream)


# --- 1. a routed turn streams to the person ------------------------------------------------------


def test_a_routed_turn_streams_its_reply_to_the_persons_feed() -> None:
    events = _Events()
    runner = _runner(_words("Hoy vendiste 120 piezas. ", "Vas bien."), events)

    outcome = asyncio.run(runner.run(TURN, _request(), PROFILE, None))

    assert outcome.result is not None
    assert "".join(d.text or "" for d in events.deltas) == outcome.result.text
    assert {d.turn_id for d in events.deltas} == {str(TURN)}
    assert all(d.owner is None and d.session_id == "s" for d in events.deltas)


def test_a_routed_intent_plans_compose_streams_its_reply_to_the_persons_feed() -> None:
    events = _Events()
    runner = _runner(_words("Tienes 2 unidades. ", "Pide hoy."), events)

    outcome = asyncio.run(runner.compose_reply(TURN, _request(), PROFILE, RESULTS))

    assert outcome.result is not None and outcome.result.text == "Tienes 2 unidades. Pide hoy."
    assert [d.text for d in events.deltas] == ["Tienes 2 unidades. ", "Pide hoy."]
    assert events.reply_text() == outcome.result.text, "a complete compose takes nothing back"


# --- 2. a peer turn never streams ----------------------------------------------------------------


@pytest.mark.parametrize("routed", [True, False], ids=["delegated", "person_identity"])
def test_a_peer_turn_never_streams(routed: bool) -> None:
    events = _Events()
    runner = _runner(_words("Hello there. ", "Bye."), events)

    asyncio.run(runner.run(TURN, _request(routed=routed, peer=True), PROFILE, None))
    asyncio.run(runner.compose_reply(TURN, _request(routed=routed, peer=True), PROFILE, RESULTS))

    assert events.events == []


# --- 3. a compose that is not a reply takes its streamed text back -------------------------------


def test_a_compose_cut_at_its_token_bound_takes_back_the_text_it_streamed() -> None:
    events = _Events()
    runner = _runner(_cut("Producto A tiene 3. ", "Producto B tie"), events)

    with pytest.raises(ComposeIncompleteError):
        asyncio.run(runner.compose_reply(TURN, _request(), PROFILE, RESULTS))

    assert [d.text for d in events.deltas][:2] == ["Producto A tiene 3. ", "Producto B tie"]
    withdrawal = events.deltas[-1]
    assert withdrawal.text == ""
    run, response, index = (withdrawal.detail or "").split(":")
    first_run, first_response, _ = (events.deltas[0].detail or "").split(":")
    assert (run, index) == (first_run, "0") and int(response) == int(first_response) + 1
    assert events.reply_text() == "", "the feed no longer shows the cut text"


def test_a_compose_that_fails_while_streaming_takes_back_the_text_it_streamed() -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        yield "Producto A tiene 3. "
        raise RuntimeError("the provider dropped the stream")

    events = _Events()
    runner = _runner(FunctionModel(stream_function=stream), events)

    with pytest.raises(RuntimeError):
        asyncio.run(runner.compose_reply(TURN, _request(), PROFILE, RESULTS))

    assert events.deltas[-1].text == ""
    assert events.reply_text() == ""


def test_a_compose_that_streamed_nothing_publishes_nothing_when_it_fails() -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        raise RuntimeError("the provider refused the request")
        yield ""  # pragma: no cover - makes this an async generator

    events = _Events()
    runner = _runner(FunctionModel(stream_function=stream), events)

    with pytest.raises(RuntimeError):
        asyncio.run(runner.compose_reply(TURN, _request(), PROFILE, RESULTS))

    assert events.events == [], "no withdrawal chunk when no text was streamed"
