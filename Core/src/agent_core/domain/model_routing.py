"""Which model runs which kind of step (C24, D56). Pure: no I/O, no framework.

A profile names one model for the agent. Some of what an agent does is mechanical (a specialist
asked a narrow question looks something up, calls one tool and formats the result) and a smaller,
faster model does it as well for a fraction of the latency. `AgentProfile.model_fast` names that
model and `AgentProfile.fast_steps` the kinds of step it takes; this module names the kinds and
decides which kind a request is.

Only the kinds listed here can be configured. A person's own turn is deliberately NOT one of them:
it is the conversation the person judges the agent by, and the profile's main model stays on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from agent_core.domain.turn import TurnRequest

__all__ = ["REASONING_EFFORTS", "ModelTuning", "StepKind", "step_kind"]


class StepKind(StrEnum):
    PEER_ANSWER = "peer_answer"
    """A turn another agent started to answer its question (a peer ask, foreground or
    background)."""


def step_kind(request: TurnRequest) -> StepKind | None:
    """The kind of step a request is, or None for an ordinary turn (a person or a channel
    started it), which always runs on the profile's main model."""
    return StepKind.PEER_ANSWER if request.owner is not None else None


REASONING_EFFORTS: Final = frozenset({"minimal", "low", "medium", "high"})
"""The OpenAI-compatible `reasoning_effort` values a profile may ask for."""


@dataclass(frozen=True, slots=True)
class ModelTuning:
    """A profile's `model_settings:` block (D99): how its model calls are bounded.

    `reasoning_effort` asks a reasoning model to think less (or more); `max_tokens` bounds one
    response, reasoning included on a provider that counts it there. None sends nothing, which is
    the call the Core always made. Measured on a reasoning model (L24): a specialist's answer with
    no effort spent 1.7k reasoning tokens (36 s), with `low` 0.5k (14 s) for the same facts.
    """

    reasoning_effort: str | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.reasoning_effort is not None and (
            not isinstance(self.reasoning_effort, str)
            or self.reasoning_effort not in REASONING_EFFORTS
        ):
            raise ValueError(
                f"reasoning_effort must be one of {sorted(REASONING_EFFORTS)}, "
                f"got {self.reasoning_effort!r}"
            )
        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens < 1
        ):
            raise ValueError(f"max_tokens must be a positive integer, got {self.max_tokens!r}")
