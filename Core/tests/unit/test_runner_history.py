"""The F5 half of the Pydantic AI runner: `ProcessHistory` wiring - t-f5-07.

Phase:   F5
Tasks:   docs/TASKS.md#t-f5-07
Covers:  adapters/driven/agent_pydantic/runner.py (the ContextEngine seat and the
         history-processing capability it attaches)

WHAT THIS MODULE PINS AND WHY IT IS NOT tests/unit/test_compaction.py
    `test_compaction.py` pins the LADDER: what a rung does to a history. This module pins
    the ROUTE - that the history a model request carries came back from the
    `ContextEngine`, and not from around it. An engine that compacts perfectly and is
    never reached compacts nothing, and no assertion in the ladder's own tests can see the
    difference.

THE TWO ASSERTIONS THAT ARE THE ANCHOR
    1. The engine is consulted BEFORE the model call, and what it returns is what the
       model receives. `ScriptedModel` records the messages of the first request, so the
       claim is about what the provider was actually handed.
    2. A compaction that cut mid-exchange is REFUSED, not sent. CLAUDE.md non-negotiable
       #5: a tool call separated from its return makes the provider reject the whole
       conversation with a 400, and it surfaces far from the compaction that caused it.
       The refusal is loud on purpose - falling back to the uncompacted history would
       leave a broken ladder producing a correct-looking, silently un-compacting agent,
       which is exactly the failure mode CLAUDE.md's silent-bug table is about.

NO MODEL, NO NETWORK, NO DATABASE
    `FunctionModel` stands in for the provider and a scripted `ContextEngine` stands in
    for the ladder, so every assertion here is about the wiring rather than the strategy.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any, cast

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

# Imported as a MODULE, exactly as tests/unit/test_runner.py explains: while a target in
# the file is still a stub, a name-level import turns "not implemented yet" into a
# collection-time ImportError instead of a red assertion inside a test that ran.
from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.domain.compaction import (
    CompactionPolicy,
    CompactionResult,
    ContextState,
    Rung,
)
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

pytestmark = [pytest.mark.phase("F5"), pytest.mark.silent]

TURN_ID = TurnId("turn-f5-1")
TOOL_NAME = "lookup_delivery_code"
TOOL_CALL_ID = "call-ord-42"


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="http",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator"}),
    )


def _session() -> SessionRef:
    return SessionRef(session_id="s-1", tenant_id=TenantId("t-1"))  # type: ignore[arg-type]


def _request(text: str = "And what about order ord-43?") -> TurnRequest:
    return TurnRequest(
        session=_session(),
        caller=_caller(),
        profile_id="delivery",
        input=UserInput(text=text),
    )


def _profile(**overrides: Any) -> AgentProfile:
    data: dict[str, Any] = {
        "id": "delivery",
        "persona": "You are a delivery desk assistant.",
        "model": "minimax/MiniMax-M3",
    }
    data.update(overrides)
    return AgentProfile.from_mapping(data)


def _allowing_policy() -> fakes.FakeToolPolicy:
    rule = PolicyRule(
        rule_id="r-delivery-lookup",
        tool_pattern=TOOL_NAME,
        effect=Effect.ALLOW,
        reason="the delivery desk may read a delivery code",
    )
    return fakes.FakeToolPolicy(RuleSet.for_caller(_caller(), (rule,)))


def _user(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _assistant(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def _tool_call(call_id: str = TOOL_CALL_ID) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(TOOL_NAME, {"order_id": "ord-42"}, call_id)])


def _tool_return(call_id: str = TOOL_CALL_ID) -> ModelRequest:
    return ModelRequest(
        parts=[ToolReturnPart(tool_name=TOOL_NAME, content="DLV-1", tool_call_id=call_id)]
    )


def _paired_history() -> list[ModelMessage]:
    """One complete exchange: ask, tool call, tool return, answer. Plus an older one."""
    return [
        _user("Hello"),
        _assistant("Hello - how can I help?"),
        _user("What is the delivery code for order ord-42?"),
        _tool_call(),
        _tool_return(),
        _assistant("It is DLV-1."),
    ]


def _prompt_texts(messages: Sequence[ModelMessage]) -> list[str]:
    """The user-visible spine of a history, which is what a compaction changes."""
    return [
        str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


class ScriptedModel:
    """A `FunctionModel` body that records what the PROVIDER was handed, first request on.

    Assertions about compaction have to be about the messages that went out; a test that
    inspects what the runner believes it sent cannot tell a wired engine from a bypassed
    one.
    """

    __name__ = "scripted_model"

    def __init__(self, final_text: str = "done") -> None:
        self.final_text = final_text
        self.requests = 0
        self.history_seen: list[ModelMessage] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests += 1
        if self.requests == 1:
            self.history_seen = list(messages)
        return ModelResponse(
            parts=[TextPart(self.final_text)],
            usage=RequestUsage(input_tokens=13, output_tokens=5),
        )


class ScriptedContextEngine:
    """A `ContextEngine` whose trigger and whose ladder output are both dictated.

    It records the `ContextState` it was asked about and how many model requests had
    already happened when it was asked, so "before the model call" is a recorded fact
    rather than an ordering the test hopes for.
    """

    def __init__(
        self,
        model: ScriptedModel,
        *,
        should_compress: bool,
        compacted: Sequence[ModelMessage] | None = None,
    ) -> None:
        self._model = model
        self._should = should_compress
        self._compacted: list[ModelMessage] = list(compacted or [])
        self.states: list[ContextState] = []
        self.requests_before_trigger: list[int] = []
        self.compress_calls: list[list[ModelMessage]] = []

    def on_session_start(self, session: SessionRef) -> None: ...

    def update_from_response(self, session: SessionRef, usage: Any) -> None: ...

    def should_compress(self, state: ContextState, policy: CompactionPolicy) -> bool:
        self.states.append(state)
        self.requests_before_trigger.append(self._model.requests)
        return self._should

    async def compress(
        self, session: SessionRef, history: object, policy: CompactionPolicy
    ) -> CompactionResult:
        self.compress_calls.append(list(cast("Sequence[ModelMessage]", history)))
        return CompactionResult(
            compacted_history=list(self._compacted),
            checkpoint=None,
            rungs_applied=(Rung.L2_SLIDING_WINDOW,),
            tokens_before=1_000,
            tokens_after=100,
        )

    def on_session_end(self, session: SessionRef) -> None: ...


def _model_factory(scripted: ScriptedModel) -> Any:
    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(scripted)

    return factory


def _runner(*, scripted: ScriptedModel, context: ScriptedContextEngine | None = None) -> Any:
    kwargs: dict[str, Any] = {
        "model": fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        "policy": _allowing_policy(),
        "audit": fakes.FakeAuditSink(),
        "model_factory": _model_factory(scripted),
    }
    if context is not None:
        # An assertion rather than a TypeError from the constructor, so the missing seat
        # reads as the anchor it belongs to instead of as a broken test.
        parameters = inspect.signature(runner_module.PydanticAgentRunner.__init__).parameters
        assert "context" in parameters, (
            "PydanticAgentRunner has no `context` seat, so a ContextEngine cannot reach "
            "the model request through the runner - docs/TASKS.md#t-f5-07"
        )
        kwargs["context"] = context
    return runner_module.PydanticAgentRunner(**kwargs)


@pytest.mark.asyncio
async def test_history_reaches_the_model_through_the_engine_not_around_it() -> None:
    """What the engine returned is what the provider was handed, and it was asked first."""
    scripted = ScriptedModel()
    compacted: list[ModelMessage] = [
        _user("[summary] the caller asked about order ord-42; code DLV-1.")
    ]
    engine = ScriptedContextEngine(scripted, should_compress=True, compacted=compacted)
    runner = _runner(scripted=scripted, context=engine)

    await runner.run(TURN_ID, _request(), _profile(), _paired_history())

    assert engine.compress_calls, "the engine was never asked to compress"
    # Consulted BEFORE the model call: no request had been made when the trigger ran.
    assert engine.requests_before_trigger[0] == 0
    assert engine.states[0].session == _session()
    # The engine is handed the history AS THE REQUEST WOULD CARRY IT - this turn's own
    # prompt included - which is what makes it a compaction of the request rather than of
    # whatever was on disk before the turn began.
    assert _prompt_texts(engine.compress_calls[0]) == [
        *_prompt_texts(_paired_history()),
        "And what about order ord-43?",
    ]
    # And the provider saw exactly what came back, never the six messages that went in.
    assert _prompt_texts(scripted.history_seen) == _prompt_texts(compacted)


@pytest.mark.asyncio
async def test_a_compaction_that_cut_mid_exchange_is_refused_rather_than_sent() -> None:
    """CLAUDE.md #5: a call without its return must never reach the provider."""
    scripted = ScriptedModel()
    # The tool RETURN is dropped and the call that asked for it is kept: the exact cut
    # that makes a provider reject the whole conversation with a 400.
    orphaned: list[ModelMessage] = [
        _user("What is the delivery code for order ord-42?"),
        _tool_call(),
        _assistant("It is DLV-1."),
    ]
    engine = ScriptedContextEngine(scripted, should_compress=True, compacted=orphaned)
    runner = _runner(scripted=scripted, context=engine)

    with pytest.raises(runner_module.MidExchangeCompactionError) as refused:
        await runner.run(TURN_ID, _request(), _profile(), _paired_history())

    assert TOOL_CALL_ID in str(refused.value)
    assert scripted.requests == 0, "the broken history reached the provider anyway"


@pytest.mark.asyncio
async def test_a_tool_return_left_without_its_call_is_refused_too() -> None:
    """The other half of the pair. A return whose call was cut breaks the same way."""
    scripted = ScriptedModel()
    orphaned: list[ModelMessage] = [
        _user("What is the delivery code for order ord-42?"),
        _tool_return(),
    ]
    engine = ScriptedContextEngine(scripted, should_compress=True, compacted=orphaned)
    runner = _runner(scripted=scripted, context=engine)

    with pytest.raises(runner_module.MidExchangeCompactionError):
        await runner.run(TURN_ID, _request(), _profile(), _paired_history())

    assert scripted.requests == 0


def test_a_history_that_arrived_unpaired_is_not_blamed_on_the_compactor() -> None:
    """Only a pairing the COMPACTION broke is refused.

    A history that was already unpaired when it arrived is somebody else's defect, and
    refusing it here would turn this guard into a second, wrong owner of that bug - and
    would make a deferred call (F3), which legitimately has no return yet, unrunnable.

    ASSERTED AGAINST THE GUARD RATHER THAN THROUGH `run`, and that is a fact about the
    library worth writing down: Pydantic AI repairs a dangling tool call by synthesizing a
    return BEFORE any capability sees the history, so an already-unpaired history cannot
    reach the processor through `agent.run` at all. The comparison still has to be
    before-versus-after, because the shapes that survive that repair - a deferred call in
    F3 - arrive unpaired by design.
    """
    arrived: list[ModelMessage] = [
        _user("Look up ord-42"),
        _tool_call(),
        _assistant("It is DLV-1."),
    ]

    # Same orphan in, same orphan out: the compaction added nothing to blame.
    runner_module._refuse_if_compaction_broke_pairing(arrived, list(arrived))

    with pytest.raises(runner_module.MidExchangeCompactionError):
        runner_module._refuse_if_compaction_broke_pairing(_paired_history(), arrived)


@pytest.mark.asyncio
async def test_an_engine_that_says_no_leaves_the_history_alone() -> None:
    """The runner obeys the trigger and never second-guesses it in either direction."""
    scripted = ScriptedModel()
    engine = ScriptedContextEngine(scripted, should_compress=False)
    runner = _runner(scripted=scripted, context=engine)
    history = _paired_history()

    await runner.run(TURN_ID, _request(), _profile(), history)

    assert engine.compress_calls == []
    assert _prompt_texts(scripted.history_seen) == [
        *_prompt_texts(history),
        "And what about order ord-43?",
    ]


@pytest.mark.asyncio
async def test_no_engine_wired_is_still_the_f1_turn() -> None:
    """F1 ships without a ContextEngine and must keep running when F5 is not wired."""
    scripted = ScriptedModel()
    runner = _runner(scripted=scripted)

    outcome = await runner.run(TURN_ID, _request(), _profile(), _paired_history())

    assert outcome.result is not None
    assert scripted.requests == 1
