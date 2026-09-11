"""The F3 half of the Pydantic AI runner: `resume()` - t-f3-10.

Phase:   F3
Tasks:   docs/TASKS.md#t-f3-10
Covers:  adapters/driven/agent_pydantic/runner.py (PydanticAgentRunner.resume)

WHAT THIS MODULE PINS AND WHY IT IS NOT tests/unit/test_resume_turn.py
    `test_resume_turn.py` pins the USE CASE - which resolutions reach the runner, and that
    an answered pair never reaches it twice. This module pins the ADAPTER - that the answer
    a human gave from outside arrives at the provider as the pending call's result, under
    the id the provider itself issued.

    Nothing else in the suite can see that. `ResumeTurn` hands the runner a
    `tool_call_id` and never learns what became of it; the provider is the only party that
    notices a wrong one, and it notices with a 400 on some later request.

THE TWO ASSERTIONS THAT ARE THE ANCHOR
    1. The id ROUND-TRIPS BYTE FOR BYTE. `tool_call_id` is the provider's string, not
       ours. Pydantic AI matches a supplied result to a pending call by that string, so a
       regenerated - or merely re-cased - id binds to nothing. CLAUDE.md's silent-bug
       table: tool call/return pairing surfaces as a provider 400, much later, never here.
       The id below is deliberately mixed-case with underscores and a hyphen so a
       normalising adapter cannot pass by accident.
    2. An id the history has NO pending call for is REFUSED, not guessed at. Pydantic AI
       2.31 does raise its own `UserError` on that shape, but it raises one that talks
       about "all deferred tool calls" and prints two id sets - it cannot say which side
       invented the id, because by then it no longer knows. Refusing here names the
       offending id at the boundary that received it, and refuses BEFORE the provider is
       reached.

NO MODEL, NO NETWORK, NO DATABASE
    `FunctionModel` stands in for the provider, exactly as `test_runner.py` does it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

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
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

# Imported as a MODULE, for the reason test_runner.py states: while the target is still a
# stub, a name-level import turns "not implemented yet" into a collection-time ImportError
# instead of a red inside a test that actually ran.
from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
)
from tests.fakes import ports as fakes

pytestmark = [pytest.mark.phase("F3"), pytest.mark.silent]

TURN_ID = TurnId("turn-f3-1")
TOOL_NAME = "lookup_delivery_code"

# The t-f3-16 seats. `resume` carries them because a resumed continuation is policed,
# audited and compacted like any other - `tests/unit/test_runner_resume_identity.py` is
# where that is asserted. Here they are simply supplied: this module is about the id
# round-trip, and a call that could omit them would no longer be the call production makes.
SESSION = SessionRef(session_id=SessionId("s-f3-1"), tenant_id=TenantId("t-1"))

# A provider-shaped id, chosen to be hostile to every plausible "tidy-up": mixed case, an
# underscore run, a hyphen, and nothing that parses as a UUID. An adapter that lowercases,
# strips, re-cases or regenerates fails on this string and passes on a tame one.
PROVIDER_TOOL_CALL_ID = "call_Ab3-XY_9__MiXeD"

# What the human supplied from outside. Unique enough that finding it in what the model
# received proves it travelled, rather than being reconstructed by the assertion.
OUTSIDE_ANSWER = "DLV-ANSWERED-FROM-OUTSIDE-7714"

# What the tool itself would return if it ran. Distinct from OUTSIDE_ANSWER so "the tool
# ran" and "the outside answer came back" are never confused for each other.
TOOL_BODY_RESULT = "DLV-FROM-THE-TOOL-BODY"


@dataclass
class Resolution:
    """One human answer, in the shape `ports/agent_runner.py::ToolResolution` declares.

    Built here rather than imported from `application/resume_turn.py`: this is an adapter
    test, and the adapter's contract is the structural Protocol, not one use case's
    concrete carrier.
    """

    tool_call_id: ToolCallId
    approved: bool
    payload: object | None = None


class ScriptedModel:
    """A `FunctionModel` body that records what the provider was actually handed."""

    __name__ = "scripted_resume_model"

    def __init__(self, final_text: str = "The delivery code is confirmed.") -> None:
        self.final_text = final_text
        self.requests = 0
        self.tool_call_ids_returned: list[str] = []
        self.tool_results_seen: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests += 1
        for message in messages:
            for part in message.parts:
                if isinstance(part, ToolReturnPart):
                    self.tool_call_ids_returned.append(part.tool_call_id)
                    self.tool_results_seen.append(str(part.content))
        return ModelResponse(
            parts=[TextPart(self.final_text)],
            usage=RequestUsage(input_tokens=17, output_tokens=4),
        )


class DeliveryDesk:
    """The one tool. Records its calls so "the body never ran" is a fact, not a hope."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup_delivery_code(self, order_id: str) -> str:
        """Return the internal delivery code for one order id."""
        self.calls.append(order_id)
        return TOOL_BODY_RESULT


def _profile(**overrides: Any) -> AgentProfile:
    data: dict[str, Any] = {
        "id": "delivery",
        "persona": "You are a delivery desk assistant.",
        "model": "minimax/MiniMax-M3",
    }
    data.update(overrides)
    return AgentProfile.from_mapping(data)


def _model_factory(scripted: ScriptedModel) -> Any:
    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(scripted)

    return factory


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-1",
        channel="http",
        tenant_id=TenantId("t-1"),
        roles=frozenset({"operator"}),
    )


def _runner(*, scripted: ScriptedModel, desk: DeliveryDesk | None = None) -> Any:
    tools = None
    if desk is not None:
        tools = fakes.FakeToolProvider(
            FunctionToolset([desk.lookup_delivery_code]), (TOOL_NAME,)
        )
    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        # ALLOW by default, and that is a consequence of t-f3-16 rather than a
        # convenience. Now that `resume` carries the caller, the resumed continuation is
        # POLICED - so an empty rule set, which defaults to DENY, would refuse the very
        # tool this module needs to execute and every assertion here would be about a
        # denial instead of about the id round-trip. Whether a resumed call is policed at
        # all is `tests/unit/test_runner_resume_identity.py`'s subject, and it proves it
        # with an explicit DENY rule rather than with a default.
        policy=fakes.FakeToolPolicy(
            RuleSet.for_caller(_caller(), (), default_effect=Effect.ALLOW)
        ),
        audit=fakes.FakeAuditSink(),
        tools=tools,
        model_factory=_model_factory(scripted),
    )


def _suspended_history(tool_call_id: str = PROVIDER_TOOL_CALL_ID) -> list[ModelMessage]:
    """A turn frozen mid-exchange: the model asked for a tool, nothing answered it yet.

    This is what `ConversationStore.load_history` hands back for a suspended turn, and it
    is unpaired BY DESIGN - the pending call is the thing a human is being asked about.
    """
    return [
        ModelRequest(parts=[UserPromptPart("What is the delivery code for order ord-42?")]),
        ModelResponse(
            parts=[
                ToolCallPart(TOOL_NAME, {"order_id": "ord-42"}, tool_call_id=tool_call_id)
            ],
            usage=RequestUsage(input_tokens=11, output_tokens=7),
        ),
    ]


# --------------------------------------------------------------------------------------
# 1. The answer from outside comes back as the pending call's result, under the
#    provider's own id


def test_the_outside_answer_is_fed_back_under_the_providers_tool_call_id_verbatim() -> None:
    """The whole anchor in one assertion pair: the payload arrives, and the id is untouched.

    An EVIDENCE-shaped resolution (`approved=True` with a payload) is the case that proves
    the most: the tool body must NOT run, because the human supplied the result the model
    is to receive. If the adapter fed this back as an approval instead, the desk would
    execute and the model would see `TOOL_BODY_RESULT` - a difference no id assertion
    alone would catch.
    """
    scripted = ScriptedModel()
    desk = DeliveryDesk()
    runner = _runner(scripted=scripted, desk=desk)

    outcome = asyncio.run(
        runner.resume(
            TURN_ID,
            _profile(),
            _suspended_history(),
            (
                Resolution(
                    tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID),
                    approved=True,
                    payload=OUTSIDE_ANSWER,
                ),
            ),
            caller=_caller(),
            session=SESSION,
        )
    )

    # The id the provider issued is the id the provider got back. Byte for byte: `==` on
    # the exact string, never a case-insensitive or stripped comparison, because a
    # normalising adapter is exactly what this test exists to fail.
    assert scripted.tool_call_ids_returned == [PROVIDER_TOOL_CALL_ID]
    assert scripted.tool_results_seen == [OUTSIDE_ANSWER]
    assert desk.calls == [], "the human supplied the result; the tool body must not run"

    assert outcome.turn_id == TURN_ID
    assert outcome.result is not None
    assert outcome.result.text == scripted.final_text


def test_an_approval_with_no_payload_runs_the_tool_under_the_same_id() -> None:
    """The other half of D9's single flow: an approval executes the deferred call itself.

    Same id discipline, different meaning - here the human authorised the call rather than
    answering it, so the tool body IS the result. The id still has to round-trip, because
    the return Pydantic AI builds from the executed tool carries it.
    """
    scripted = ScriptedModel()
    desk = DeliveryDesk()
    runner = _runner(scripted=scripted, desk=desk)

    asyncio.run(
        runner.resume(
            TURN_ID,
            _profile(),
            _suspended_history(),
            (Resolution(tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID), approved=True),),
            caller=_caller(),
            session=SESSION,
        )
    )

    assert desk.calls == ["ord-42"]
    assert scripted.tool_call_ids_returned == [PROVIDER_TOOL_CALL_ID]
    assert scripted.tool_results_seen == [TOOL_BODY_RESULT]


def test_a_refusal_reaches_the_model_as_the_result_and_never_runs_the_tool() -> None:
    """`approved=False` carries the human's reason to the model, under the same id.

    `ports/agent_runner.py`: "`approved=False` means the tool must NOT run; `payload` then
    carries the reason shown to the model." The reason exists so the agent can adapt
    instead of retrying blindly - dropping it would leave the model guessing.
    """
    scripted = ScriptedModel()
    desk = DeliveryDesk()
    runner = _runner(scripted=scripted, desk=desk)
    reason = "The delivery desk operator declined: order ord-42 is under dispute."

    asyncio.run(
        runner.resume(
            TURN_ID,
            _profile(),
            _suspended_history(),
            (
                Resolution(
                    tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID),
                    approved=False,
                    payload=reason,
                ),
            ),
            caller=_caller(),
            session=SESSION,
        )
    )

    assert desk.calls == []
    assert scripted.tool_call_ids_returned == [PROVIDER_TOOL_CALL_ID]
    assert scripted.tool_results_seen == [reason]


# --------------------------------------------------------------------------------------
# 2. An id the history has no pending call for is refused, not guessed at


def test_a_resume_naming_an_unknown_tool_call_id_is_refused_before_the_provider() -> None:
    """A re-cased id is a DIFFERENT id, and this is the shape that actually happens.

    Somewhere between the provider and the human's answer, something lowercases an
    identifier - a URL path, a database column, a JSON round-trip through a client that
    normalises keys. The adapter must not accept it by matching loosely, and must not
    quietly drop it either: a dropped resolution leaves the turn suspended forever and the
    agent asking the same question with no exception anywhere.
    """
    scripted = ScriptedModel()
    desk = DeliveryDesk()
    runner = _runner(scripted=scripted, desk=desk)
    normalised = PROVIDER_TOOL_CALL_ID.lower()

    with pytest.raises(runner_module.UnknownToolCallIdError) as refused:
        asyncio.run(
            runner.resume(
                TURN_ID,
                _profile(),
                _suspended_history(),
                (
                    Resolution(
                        tool_call_id=ToolCallId(normalised),
                        approved=True,
                        payload=OUTSIDE_ANSWER,
                    ),
                ),
                caller=_caller(),
                session=SESSION,
            )
        )

    # It names the id it was given AND the one it is pending on, so the caller can see the
    # difference is the casing rather than a missing request.
    message = str(refused.value)
    assert normalised in message
    assert PROVIDER_TOOL_CALL_ID in message

    assert scripted.requests == 0, "the unknown id reached the provider anyway"
    assert desk.calls == []


def test_an_unknown_id_smuggled_in_beside_a_valid_one_is_refused_too() -> None:
    """The dangerous shape: one real answer carrying one invented id.

    Accepting the batch because most of it matched would run the genuine call while the
    invented one binds to nothing - a half-resumed turn, which is the state with no owner.
    """
    scripted = ScriptedModel()
    runner = _runner(scripted=scripted, desk=DeliveryDesk())

    with pytest.raises(runner_module.UnknownToolCallIdError) as refused:
        asyncio.run(
            runner.resume(
                TURN_ID,
                _profile(),
                _suspended_history(),
                (
                    Resolution(
                        tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID), approved=True
                    ),
                    Resolution(tool_call_id=ToolCallId("call_never_issued"), approved=True),
                ),
                caller=_caller(),
                session=SESSION,
            )
        )

    assert "call_never_issued" in str(refused.value)
    assert scripted.requests == 0


def test_a_history_with_nothing_pending_refuses_instead_of_resuming() -> None:
    """An already-answered call is not a pending one, and neither is an empty history.

    `ResumeTurn` drops pairs it has already resolved, so a batch that arrives here fully
    answered is a caller that skipped that guard - or a signal redelivered to the wrong
    turn. Either way there is no suspension to resume, and resuming anyway would replay a
    tool the conversation already has a result for.
    """
    scripted = ScriptedModel()
    runner = _runner(scripted=scripted)
    answered: list[ModelMessage] = [
        *_suspended_history(),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name=TOOL_NAME,
                    content=TOOL_BODY_RESULT,
                    tool_call_id=PROVIDER_TOOL_CALL_ID,
                )
            ]
        ),
    ]

    with pytest.raises(runner_module.UnknownToolCallIdError):
        asyncio.run(
            runner.resume(
                TURN_ID,
                _profile(),
                answered,
                (Resolution(tool_call_id=ToolCallId(PROVIDER_TOOL_CALL_ID), approved=True),),
                caller=_caller(),
                session=SESSION,
            )
        )

    assert scripted.requests == 0
