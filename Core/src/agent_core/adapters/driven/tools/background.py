"""The seat the background-agent tools reach their conversation's background asks (C20, D50).

`peer_list`, `peer_status`, `peer_message` and `peer_stop` (`tools/peers.py`) are plain tools: the
model calls them and gets text back. What they act ON is the background asks of the conversation
the turn belongs to, and acting on them needs the mailbox, the peer gate and the workflow engine
(a stop cancels the callee's workflow). The tools know none of that: they hold a `BackgroundAgents`
and the `BackgroundOwner` of the turn, both handed in by the runner, and the implementation lives
with the workflow (`adapters/driving/workflow/background_agents.py`), the one package allowed to
touch the engine.

NOT a port, for the reason `PendingInputs` and `AskerContext` are not: one consumer, one question.

EVERY VERB IS SCOPED TO THE OWNER CONVERSATION (tenant AND session id). A handle is a correlation
id, which anybody who has seen one could type; the scope is what makes a handle from another
conversation exactly as unknown as one that never existed, and the caller cannot tell the two apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from agent_core.domain.peers import BackgroundAsk
from agent_core.domain.turn import CallerIdentity, SessionRef

__all__ = [
    "BackgroundAgents",
    "BackgroundOwner",
    "MessageMode",
    "MessageResult",
    "StopResult",
]


class MessageMode(StrEnum):
    """How `peer_message` addresses a running child.

    STEER     - course-correct work already under way ("use the March invoice, not February").
    FOLLOW_UP - add a question or a task for after the current work.

    Both are delivered the same way: as a new turn in the child's own session, queued behind the
    turn it is running (a turn is one recorded model step at a time and cannot be interrupted
    mid-flight). The mode is the framing the child reads (D50).
    """

    STEER = "steer"
    FOLLOW_UP = "follow_up"


class StopResult(StrEnum):
    """What `peer_stop` did."""

    STOPPED = "stopped"
    NOT_RUNNING = "not_running"  # finished, failed or already stopped: nothing to cancel
    UNKNOWN = "unknown"  # no such handle in THIS conversation


@dataclass(frozen=True, slots=True)
class BackgroundOwner:
    """Whose conversation is asking, and from which turn.

    Built by the runner from the turn it is running, never from anything the model wrote. `caller`
    is the identity a message's answer is delivered to (the same snapshot a background ask keeps)
    and `turn_id` the asking turn. The depth and the root a message travels with are read off the
    child's own row (a resumed turn has neither at hand).
    """

    session: SessionRef
    profile_id: str
    caller: CallerIdentity
    turn_id: str


@dataclass(frozen=True, slots=True)
class MessageResult:
    """The outcome of `peer_message`: the handle of the message (its answer arrives like any
    background answer) or the reason it was not sent. Exactly one of the two is set."""

    handle: str | None = None
    refusal: str | None = None


class BackgroundAgents(Protocol):
    async def list(self, owner: BackgroundOwner, *, limit: int) -> tuple[BackgroundAsk, ...]:
        """The newest `limit` background asks of the owner conversation."""
        ...

    async def count(self, owner: BackgroundOwner) -> int:
        """How many background asks the owner conversation has, however many a listing shows."""
        ...

    async def status(self, owner: BackgroundOwner, handle: str) -> BackgroundAsk | None: ...

    async def stop(self, owner: BackgroundOwner, handle: str) -> StopResult: ...

    async def message(
        self,
        owner: BackgroundOwner,
        handle: str,
        text: str,
        mode: MessageMode,
        *,
        call_id: str,
    ) -> MessageResult: ...
