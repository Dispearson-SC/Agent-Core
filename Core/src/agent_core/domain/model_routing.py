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

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.domain.turn import TurnRequest

__all__ = ["StepKind", "step_kind"]


class StepKind(StrEnum):
    PEER_ANSWER = "peer_answer"
    """A turn another agent started to answer its question (a peer ask, foreground or
    background)."""


def step_kind(request: TurnRequest) -> StepKind | None:
    """The kind of step a request is, or None for an ordinary turn (a person or a channel
    started it), which always runs on the profile's main model."""
    return StepKind.PEER_ANSWER if request.owner is not None else None
