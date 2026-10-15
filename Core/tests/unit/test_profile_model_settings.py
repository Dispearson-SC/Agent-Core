"""A profile's `model_settings:` block - the reasoning effort and output bound of its model calls.

Measured on a reasoning model (see `docs/DECISIONS.md`, D99): without an effort a specialist's
answer spent 1.7k reasoning tokens (36 s) where `low` spent 0.5k (14 s) for the same facts. The
block is optional and absent means exactly the call the Core always made.
"""

from __future__ import annotations

import pytest

from agent_core.domain.model_routing import ModelTuning
from agent_core.domain.profile import AgentProfile, ProfileValidationError

BASE = {"id": "p", "persona": "P", "model": "minimax/MiniMax-M3"}


def _profile(**extra: object) -> AgentProfile:
    return AgentProfile.from_mapping({**BASE, **extra})


def test_without_the_block_the_profile_sends_no_settings() -> None:
    assert _profile().model_settings is None


def test_the_block_sets_the_reasoning_effort_and_the_output_bound() -> None:
    profile = _profile(model_settings={"reasoning_effort": "low", "max_tokens": 2048})

    assert profile.model_settings == ModelTuning(reasoning_effort="low", max_tokens=2048)


def test_either_setting_may_be_left_out() -> None:
    assert _profile(model_settings={"reasoning_effort": "minimal"}).model_settings == ModelTuning(
        reasoning_effort="minimal"
    )
    assert _profile(model_settings={"max_tokens": 1}).model_settings == ModelTuning(max_tokens=1)


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"reasoning_effort": "turbo"}, "model_settings.reasoning_effort"),
        ({"reasoning_effort": 3}, "model_settings.reasoning_effort"),
        ({"max_tokens": 0}, "model_settings.max_tokens"),
        ({"max_tokens": "many"}, "model_settings.max_tokens"),
        ({"temperature": 0.2}, "temperature"),
        ("low", "model_settings"),
    ],
)
def test_a_wrong_block_is_refused_at_load(block: object, message: str) -> None:
    with pytest.raises(ProfileValidationError, match=message):
        _profile(model_settings=block)


def test_the_settings_are_part_of_the_content_hash() -> None:
    """Changing how hard the model thinks changes what the agent does: it is a new version."""
    plain = _profile()
    tuned = _profile(model_settings={"reasoning_effort": "low"})

    assert plain.content_hash != tuned.content_hash


def test_a_peer_answer_step_keeps_the_settings() -> None:
    """`for_step` swaps the model for `model_fast`; the settings stay the profile's."""
    profile = _profile(model_fast="minimax/fast", model_settings={"reasoning_effort": "low"})

    from agent_core.domain.model_routing import StepKind

    assert profile.for_step(StepKind.PEER_ANSWER).model_settings == ModelTuning(
        reasoning_effort="low"
    )
