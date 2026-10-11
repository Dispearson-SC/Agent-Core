"""The turn's channel and modality shape the agent's instructions, never the user's text.

Subject: domain/profile.py (`channel_instructions`, `modality_instructions`,
`instructions_for`), domain/turn.py (`CallerIdentity.modality`), the runner and the HTTP
route (`X-Modality`).

A vertical used to prefix the user text with "[canal: voz] " to change the reply style, and
the prefix leaked into replies. Now a profile declares per-channel / per-modality
instruction fragments and the runner appends the matching ones as instructions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driving.http.routes import create_app
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

_BASE = {"id": "p", "persona": "PERSONA", "model": "minimax/MiniMax-M3"}
_WITH_FRAGMENTS = {
    **_BASE,
    "modality_instructions": {"voice": "Reply in short spoken sentences."},
    "channel_instructions": {"whatsapp": "Keep it under 300 characters."},
}


def test_instructions_for_picks_channel_then_modality() -> None:
    profile = AgentProfile.from_mapping(_WITH_FRAGMENTS)

    assert profile.instructions_for("whatsapp", "voice") == (
        "Keep it under 300 characters.",
        "Reply in short spoken sentences.",
    )
    assert profile.instructions_for("http", "voice") == ("Reply in short spoken sentences.",)
    assert profile.instructions_for("http", None) == ()
    assert profile.instructions_for("http", "text") == ()


def test_profiles_without_fragments_are_unchanged() -> None:
    assert AgentProfile.from_mapping(_BASE).instructions_for("whatsapp", "voice") == ()


def test_fragments_move_the_content_hash() -> None:
    plain = AgentProfile.from_mapping(_BASE)
    fragmented = AgentProfile.from_mapping(_WITH_FRAGMENTS)

    assert plain.content_hash != fragmented.content_hash


def test_a_malformed_fragment_map_is_refused() -> None:
    with pytest.raises(ProfileValidationError, match="modality_instructions"):
        AgentProfile.from_mapping({**_BASE, "modality_instructions": ["voice"]})
    with pytest.raises(ProfileValidationError, match="channel_instructions"):
        AgentProfile.from_mapping({**_BASE, "channel_instructions": {"web": ""}})


# --- the runner appends the fragments as instructions, the user text is untouched -------

SESSION = SessionRef(session_id="s-1", tenant_id=TenantId("t"))  # type: ignore[arg-type]


def _caller(modality: str | None) -> CallerIdentity:
    return CallerIdentity(
        subject_id="u", channel="whatsapp", tenant_id=TenantId("t"), modality=modality
    )


@dataclass
class _Seen:
    instructions: list[str | None] = field(default_factory=list)
    user_texts: list[str] = field(default_factory=list)


def _runner(seen: _Seen, caller: CallerIdentity) -> Any:
    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.instructions.append(info.instructions)
        for message in messages:
            if isinstance(message, ModelRequest):
                seen.user_texts.extend(
                    p.content for p in message.parts if isinstance(p, UserPromptPart)
                )
        return ModelResponse(
            parts=[TextPart("ok")], usage=RequestUsage(input_tokens=1, output_tokens=1)
        )

    def factory(model_id: str, base_url: str | None) -> Model:
        return FunctionModel(scripted)

    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(caller, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(FunctionToolset([]), ()),
        model_factory=factory,
    )


def _run(modality: str | None) -> _Seen:
    seen = _Seen()
    caller = _caller(modality)
    request = TurnRequest(
        session=SESSION, caller=caller, profile_id="p", input=UserInput(text="where is it?")
    )
    asyncio.run(
        _runner(seen, caller).run(
            TurnId("t1"), request, AgentProfile.from_mapping(_WITH_FRAGMENTS), None
        )
    )
    return seen


def test_a_voice_turn_gets_the_voice_fragment_and_the_user_text_is_unchanged() -> None:
    seen = _run("voice")

    (instructions,) = seen.instructions
    assert instructions is not None
    assert "PERSONA" in instructions
    assert "Reply in short spoken sentences." in instructions
    assert "Keep it under 300 characters." in instructions  # channel fragment too
    assert seen.user_texts == ["where is it?"]


def test_a_text_turn_gets_no_voice_fragment() -> None:
    seen = _run(None)

    (instructions,) = seen.instructions
    assert instructions is not None
    assert "Reply in short spoken sentences." not in instructions
    assert seen.user_texts == ["where is it?"]


# --- the HTTP route carries the hint on the caller ---------------------------------------


@dataclass
class _Starter:
    started: list[TurnRequest] = field(default_factory=list)

    async def __call__(self, request: TurnRequest) -> Any:
        self.started.append(request)
        return type("H", (), {"turn_id": TurnId("t-1")})()


def _post(modality: str | None) -> TurnRequest:
    starter = _Starter()
    headers = {"X-Subject-Id": "u", "X-Tenant-Id": "t"}
    if modality is not None:
        headers["X-Modality"] = modality
    response = TestClient(create_app(start_turn=starter)).post(
        "/turns", json={"session_id": "s", "profile_id": "p", "text": "hola"}, headers=headers
    )
    assert response.status_code == 202
    return starter.started[0]


def test_the_route_puts_the_modality_hint_on_the_caller_not_in_the_text() -> None:
    request = _post("voice")

    assert request.caller.modality == "voice"
    assert request.input.text == "hola"


def test_no_modality_header_means_none() -> None:
    assert _post(None).caller.modality is None
