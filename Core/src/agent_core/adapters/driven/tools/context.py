"""Shared mechanism: the identity of the turn a tool is running in.

A tool is a plain function; the model chooses its arguments. Anything the MODEL must not
choose - the tenant, the conversation - therefore cannot be a parameter. The runner passes
this object as Pydantic AI `deps`, and a tool that needs it declares a first parameter
`ctx: RunContext[TurnContext]` (which the model never sees in the tool schema).

It lives under `tools/` beside `peers.py` and `evidence.py` for the same reason they do: it
is a mechanism every vertical may import, not one vertical's code. It adds no field to
`domain/` - it only carries the two domain values the runner already holds.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_core.domain.turn import CallerIdentity, SessionRef

__all__ = ["TurnContext"]


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Who is asking (`caller`, with its tenant) and which conversation (`session`)."""

    caller: CallerIdentity
    session: SessionRef
    # The profile (agent) running the turn - lets a tool label its calls by role.
    agent_id: str = ""
