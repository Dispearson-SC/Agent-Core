"""C24 (D56): a profile may name a faster model for the steps that do not need the best one.

Subjects: domain/model_routing.py (`StepKind`, `step_kind`), domain/profile.py (`model_fast`,
`fast_steps`, `AgentProfile.for_step`), application/start_turn.py (the routing seat) and the
preflight's model list.

What is defended: a profile with `model_fast` runs a peer answer on it and an ordinary turn on
`model`; a profile without it behaves exactly as before; the declaration is validated at load (a
typo cannot silently route nothing); and the fast model is named wherever a model must be
reachable (preflight), because a missing credential for it would fail only at the first peer ask.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_core.application.start_turn import StartTurn
from agent_core.domain.model_routing import StepKind, step_kind
from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.turn import (
    CallerIdentity,
    PeerOwner,
    SessionRef,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.unit.test_start_turn import FINISHED, _Wiring

BASE: dict[str, Any] = {"id": "billing", "persona": "You bill.", "model": "big-model"}


def _profile(**extra: Any) -> AgentProfile:
    return AgentProfile.from_mapping({**BASE, **extra})


def _request(session: SessionRef, caller: CallerIdentity, *, peer: bool) -> TurnRequest:
    return TurnRequest(
        session=session,
        caller=caller,
        profile_id="billing",
        input=UserInput(text="What does order 7 owe?"),
        owner=PeerOwner("root", "asker-turn", "orchestrator", "call-1") if peer else None,
    )


# --- the declaration ---


def test_a_profile_without_model_fast_is_unchanged() -> None:
    profile = _profile()

    assert profile.model_fast is None and profile.fast_steps == (StepKind.PEER_ANSWER,)
    assert profile.for_step(StepKind.PEER_ANSWER) is profile
    assert profile.for_step(None) is profile


def test_model_fast_is_read_from_the_mapping_and_changes_the_content_hash() -> None:
    plain = _profile()
    tiered = _profile(model_fast="small-model")

    assert tiered.model_fast == "small-model" and tiered.fast_steps == (StepKind.PEER_ANSWER,)
    assert tiered.content_hash != plain.content_hash, "a new capability is a new profile version"


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"model_fast": ""}, "model_fast"),
        ({"model_fast": 3}, "model_fast"),
        ({"model_fast": "small", "fast_steps": ["thinking"]}, "fast_steps"),
        ({"model_fast": "small", "fast_steps": "peer_answer"}, "fast_steps"),
        ({"fast_steps": ["peer_answer"]}, "model_fast"),
    ],
)
def test_a_malformed_tier_declaration_is_refused_at_load(
    extra: dict[str, Any], message: str
) -> None:
    with pytest.raises(ProfileValidationError, match=message):
        _profile(**extra)


# --- the routing policy ---


def test_a_peer_answer_runs_on_the_fast_model_and_other_steps_on_the_main_one() -> None:
    profile = _profile(model_fast="small-model")

    routed = profile.for_step(StepKind.PEER_ANSWER)

    assert routed.model == "small-model" and routed.id == "billing"
    assert routed.model_fast == "small-model", "the rest of the profile is untouched"
    assert profile.for_step(None).model == "big-model", "an ordinary turn keeps the main model"


def test_a_step_not_listed_in_fast_steps_is_not_routed() -> None:
    profile = _profile(model_fast="small-model", fast_steps=[])

    assert profile.for_step(StepKind.PEER_ANSWER).model == "big-model"


def test_a_turn_is_a_peer_answer_exactly_when_another_agent_owns_it(
    session: SessionRef, caller: CallerIdentity
) -> None:
    assert step_kind(_request(session, caller, peer=True)) is StepKind.PEER_ANSWER
    assert step_kind(_request(session, caller, peer=False)) is None


# --- the seat in StartTurn ---


def _run(profile: AgentProfile, request: TurnRequest, caller: CallerIdentity) -> AgentProfile:
    wiring = _Wiring(caller, FINISHED)
    wiring.use_case = StartTurn(
        runner=wiring.runner,
        tools=wiring.tools,
        policy=wiring.policy,
        store=wiring.store,
        audit=wiring.audit,
        context=wiring.context,
        skills=wiring.skills,
        profiles={profile.id: profile},
    )
    asyncio.run(wiring.use_case.execute(TurnId("turn-1"), request))
    return wiring.runner.run_calls[0][2]


def test_start_turn_hands_the_runner_the_fast_model_for_a_peer_answer(
    session: SessionRef, caller: CallerIdentity
) -> None:
    """The acceptance test of C24: a profile with `model_fast` uses it for the configured step."""
    profile = _profile(model_fast="small-model")

    peer = _run(profile, _request(session, caller, peer=True), caller)
    ordinary = _run(profile, _request(session, caller, peer=False), caller)

    assert peer.model == "small-model"
    assert ordinary.model == "big-model"


def test_start_turn_leaves_a_profile_without_model_fast_alone(
    session: SessionRef, caller: CallerIdentity
) -> None:
    profile = _profile()

    assert _run(profile, _request(session, caller, peer=True), caller) is profile


# --- the fast model must be reachable too ---


def test_preflight_checks_the_credential_of_the_fast_model_as_well() -> None:
    from types import SimpleNamespace

    from agent_core.adapters.driving.cli.preflight import _credential_checks

    profile = _profile(model="anthropic/big", model_fast="openai/small")

    checks = _credential_checks(
        SimpleNamespace(proxy_base_url=None),  # type: ignore[arg-type]
        {profile.id: profile},
        {"ANTHROPIC_API_KEY": "set"},
    )

    by_name = {check.name: check.severity for check in checks}
    assert by_name == {"ANTHROPIC_API_KEY": "ok", "OPENAI_API_KEY": "fail"}


def test_a_profile_lists_each_model_it_may_call_once() -> None:
    assert _profile().models == ("big-model",)
    assert _profile(model_fast="small").models == ("big-model", "small")
    assert _profile(model_fast="big-model").models == ("big-model",)
