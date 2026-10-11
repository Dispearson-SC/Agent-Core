"""`CallerIdentity.modality` survives the durable path and a resumed turn.

Subject: adapters/driving/workflow/turn_workflow.py (`enqueue_turn_window` ->
`run_turn_window` argument serialization), adapters/driven/agent_pydantic/runner.py
(`resume`).

The unit seam for the durable path is the serializer DBOS applies to workflow arguments:
`enqueue_turn_window` hands a `TurnRequest` to `window_queue.enqueue_async`, which stores it
through `serialize_args`, and the workflow reads it back through `deserialize_args`. A field
that did not survive that round trip would silently drop the style hint on every real turn,
while the in-process HTTP tests stayed green.
"""

from __future__ import annotations

import asyncio
from typing import Any

from dbos._serialization import DefaultSerializer, deserialize_args, serialize_args
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

SESSION = SessionRef(session_id="s-1", tenant_id=TenantId("t"))  # type: ignore[arg-type]
PROFILE = AgentProfile.from_mapping(
    {
        "id": "p",
        "persona": "PERSONA",
        "model": "minimax/MiniMax-M3",
        "modality_instructions": {"voice": "Reply in short spoken sentences."},
        "channel_instructions": {"whatsapp": "Keep it under 300 characters."},
    }
)


def _caller(modality: str | None = "voice") -> CallerIdentity:
    return CallerIdentity(
        subject_id="u", channel="whatsapp", tenant_id=TenantId("t"), modality=modality
    )


def test_modality_round_trips_through_the_serializer_dbos_applies_to_the_window_request() -> None:
    request = TurnRequest(
        session=SESSION, caller=_caller("voice"), profile_id="p", input=UserInput(text="hola")
    )

    serialized, name = serialize_args((request,), {}, None, DefaultSerializer())
    inputs = deserialize_args(serialized, name, DefaultSerializer())

    (restored,) = inputs["args"]
    assert restored == request
    assert restored.caller.modality == "voice"


# --- the resume path ----------------------------------------------------------------------

CALL_ID = "call_1"


class _Spy:
    __name__ = "spy"

    def __init__(self) -> None:
        self.instructions: list[str | None] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.instructions.append(info.instructions)
        return ModelResponse(
            parts=[TextPart("done")], usage=RequestUsage(input_tokens=1, output_tokens=1)
        )


def _resume(spy: _Spy, history: list[ModelMessage], caller: CallerIdentity) -> Any:
    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(spy)

    runner = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(caller, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        tools=None,
        model_factory=factory,
    )

    class _Resolution:
        tool_call_id = ToolCallId(CALL_ID)
        approved = True
        payload: object | None = "the answer"

    return asyncio.run(
        runner.resume(
            TurnId("t1"), PROFILE, history, (_Resolution(),), caller=caller, session=SESSION
        )
    )


def _suspended() -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart("where is it?")]),
        ModelResponse(
            parts=[ToolCallPart("ask_x", {"q": "1"}, tool_call_id=CALL_ID)],
            usage=RequestUsage(input_tokens=1, output_tokens=1),
        ),
    ]


def test_a_resumed_turn_keeps_the_channel_and_modality_fragments() -> None:
    spy = _Spy()

    _resume(spy, _suspended(), _caller("voice"))

    (instructions,) = spy.instructions
    assert instructions is not None
    assert "Reply in short spoken sentences." in instructions
    assert "Keep it under 300 characters." in instructions


def test_a_resume_without_modality_gets_no_modality_fragment() -> None:
    spy = _Spy()

    _resume(spy, _suspended(), _caller(None))

    (instructions,) = spy.instructions
    assert instructions is not None
    assert "Reply in short spoken sentences." not in instructions


def test_a_resume_never_writes_the_fragments_into_the_stored_history() -> None:
    spy = _Spy()
    history = _suspended()
    before = list(history)

    outcome = _resume(spy, history, _caller("voice"))

    texts = [
        str(getattr(part, "content", "")) for message in outcome.messages for part in message.parts
    ]
    assert not any("spoken sentences" in text or "300 characters" in text for text in texts)
    # `outcome.messages` is only what the continuation added; no user prompt is invented
    # for a resume, and the history handed in is untouched.
    assert not any(
        isinstance(p, UserPromptPart)
        for m in outcome.messages
        if isinstance(m, ModelRequest)
        for p in m.parts
    )
    assert history == before


