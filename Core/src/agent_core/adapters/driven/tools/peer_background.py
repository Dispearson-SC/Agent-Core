"""Driven adapter: manage the background agents of a conversation (C20, D50).

Phase:   Agent-comms v2, wave 3
Tests:   Core/tests/unit/test_peer_background_tools.py
Seat:    adapters/driven/tools/background.py (`BackgroundAgents`, implemented with the workflow)

Four ordinary tools, the Pi example-23 set: `peer_list`, `peer_status(handle)`,
`peer_message(handle, text, mode)` and `peer_stop(handle)`. A background ask (`ask_peer(...,
background=true)`, C19) hands the model a handle; these are what it can do with one while the child
runs.

WHY THEY ARE NOT DEFERRED LIKE `ask_peer`. `ask_peer` suspends the turn because another agent has to
run before there is a result. These need no such wait: a listing is a read, and `peer_stop` and
`peer_message` are single idempotent writes the seat performs and reports at once. They run inside
the turn's recorded model step like any tool, so a replay re-runs them (a stop twice is a no-op, a
message twice is one message: its key is the tool call id).

THEY ARE NOT A SEPARATE TOOLSET BY ACCIDENT. They are registered as their own toolset
(`peer_background`) and a profile is granted it only with `peers.allow_background: true` (see
`composition._grant_peer_mechanism`), so a profile that never asked for background asks does not
even carry the names, and `ToolPolicy` rules for them are written only where they are wanted. Each
tool is also hidden by `prepare` when the runner had no seat to give it, so a deployment without one
never offers a tool that cannot work.

WHAT THE MODEL READS IS OURS. Handles, states, targets, the times and the model's own question, in a
plain sentence: never a peer's words. An answer reaches the model only as the follow-up turn of the
conversation, inside the untrusted boundary.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic_ai import RunContext, Tool
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from agent_core.adapters.driven.tools.background import MessageMode, StopResult
from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.domain.peers import BackgroundAsk, BackgroundState

__all__ = ["AUDIT_ALLOW", "build_toolset"]

# What the audit trail stores of each call (read by `audit_repository.collect_audit_policy`, through
# the registered builder's module). The handle names the child the call was about and the mode and
# the text are what was said to it - the same reasoning that stores `ask_peer`'s `target` and
# `question` by value: without them the row says an agent messaged something and not what. A
# listing takes no argument and is decided with the empty set.
AUDIT_ALLOW: dict[str, frozenset[str]] = {
    "peer_list": frozenset(),
    "peer_status": frozenset({"handle"}),
    "peer_message": frozenset({"handle", "text", "mode"}),
    "peer_stop": frozenset({"handle"}),
}

_QUESTION_PREVIEW = 80
_LISTED = 20

_NO_SEAT = "background agents are not available in this deployment"
_UNKNOWN = "No background agent with the handle {handle!r} exists in this conversation."
_NONE = "No background agents in this conversation."

_STATE_NOTE = {
    BackgroundState.QUEUED: "waiting to start",
    BackgroundState.RUNNING: "working on it",
    BackgroundState.ANSWERED: "finished; its answer was delivered here as a message",
    BackgroundState.FAILED: "failed; the failure was delivered here as a message",
    BackgroundState.CANCELLED: "stopped",
}


def _line(ask: BackgroundAsk) -> str:
    asked = (
        ask.question
        if len(ask.question) <= _QUESTION_PREVIEW
        else (ask.question[: _QUESTION_PREVIEW - 3] + "...")
    )
    return (
        f"- handle {ask.handle}: {ask.target}, {ask.state.value} ({_STATE_NOTE[ask.state]}), "
        f"asked {ask.asked_at:%Y-%m-%d %H:%M} UTC: {asked}"
    )


async def _available(ctx: RunContext[Any], tool_def: ToolDefinition) -> ToolDefinition | None:
    """Offered only when the runner holds a seat and an owner for this turn."""
    deps = ctx.deps
    if isinstance(deps, TurnContext) and deps.background is not None and deps.background_owner:
        return tool_def
    return None


def _seat(ctx: RunContext[TurnContext]) -> Any:
    deps = ctx.deps
    if deps.background is None or deps.background_owner is None:
        raise RuntimeError(_NO_SEAT)  # `prepare` hides the tools; this is a wiring bug
    return deps.background, deps.background_owner


async def peer_list(ctx: RunContext[TurnContext]) -> str:
    """List the background agents of this conversation: their handles, who they are and whether
    they are still working. Only this conversation's are shown."""
    seat, owner = _seat(ctx)
    asks = await seat.list(owner)
    if not asks:
        return _NONE
    shown = asks[:_LISTED]
    lines = "\n".join(_line(ask) for ask in shown)
    more = "" if len(asks) <= _LISTED else f"\n(and {len(asks) - _LISTED} older ones)"
    return f"Background agents in this conversation, newest first:\n{lines}{more}"


async def peer_status(ctx: RunContext[TurnContext], handle: str) -> str:
    """Check on one background agent by the handle `ask_peer` returned.

    Args:
        handle: The handle of the background ask.
    """
    seat, owner = _seat(ctx)
    ask = await seat.status(owner, handle)
    if ask is None:
        return _UNKNOWN.format(handle=handle)
    return _line(ask)


async def peer_message(
    ctx: RunContext[TurnContext],
    handle: str,
    text: str,
    mode: Literal["steer", "follow_up"] = "follow_up",
) -> str:
    """Send a message to a background agent: `steer` to change how it does the work it was given,
    `follow_up` to add something. The agent reads it after it finishes what it is doing now (it
    cannot be interrupted mid-step), and its reply arrives later as a new message in this
    conversation, like any background answer.

    Args:
        handle: The handle of the background ask.
        text: What to tell it. It reads this and nothing else of your conversation.
        mode: `steer` or `follow_up` (default).
    """
    seat, owner = _seat(ctx)
    result = await seat.message(
        owner, handle, text, MessageMode(mode), call_id=str(ctx.tool_call_id)
    )
    if result.handle is None:
        return f"The message was not sent: {result.refusal}."
    ask = await seat.status(owner, handle)
    target = "the agent" if ask is None else ask.target
    return (
        f"Sent to {target} ({mode}). It will read it after its current work. Its reply will arrive "
        f"later as a new message in this conversation, with handle {result.handle}."
    )


async def peer_stop(ctx: RunContext[TurnContext], handle: str) -> str:
    """Stop a background agent that is no longer needed. Anything it has not finished is lost.

    Args:
        handle: The handle of the background ask.
    """
    seat, owner = _seat(ctx)
    outcome = await seat.stop(owner, handle)
    if outcome is StopResult.STOPPED:
        return f"Stopped background agent {handle}. It will not answer."
    if outcome is StopResult.NOT_RUNNING:
        return f"Background agent {handle} had already finished or been stopped; nothing to stop."
    return _UNKNOWN.format(handle=handle)


def build_toolset() -> FunctionToolset[None]:
    """The four tools, a fresh toolset per call (nothing shared across profiles or turns)."""
    return FunctionToolset(
        [
            Tool(peer_list, prepare=_available),
            Tool(peer_status, prepare=_available),
            Tool(peer_message, prepare=_available),
            Tool(peer_stop, prepare=_available),
        ]
    )
