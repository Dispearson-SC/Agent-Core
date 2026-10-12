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

from agent_core.adapters.driven.tools.background import BackgroundAgents, BackgroundOwner
from agent_core.domain.turn import CallerIdentity, SessionRef

__all__ = ["PeerEntry", "TurnContext"]


@dataclass(frozen=True, slots=True)
class PeerEntry:
    """One peer the asking agent may ask this turn: its id and a line saying what it is for."""

    agent_id: str
    summary: str


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Who is asking (`caller`, with its tenant) and which conversation (`session`)."""

    caller: CallerIdentity
    session: SessionRef
    # The profile (agent) running the turn - lets a tool label its calls by role.
    agent_id: str = ""
    # C14. The peers `ask_peer` may name THIS turn, computed from the asker's PeerPolicy, the
    # callee's policy and the caller's rules (`peers/catalogue.py`). `None` means "not computed"
    # (a runner without a profile directory) and leaves the tool in its static shape; an empty
    # tuple means nobody is available and hides the tool.
    peer_catalogue: tuple[PeerEntry, ...] | None = None
    # C12. Whether this profile may continue an earlier peer session: `ask_peer` offers the
    # `continue_session` parameter only when it is true.
    allow_continuation: bool = False
    # C19. Whether this profile may run an ask in the background: `ask_peer` offers `background`
    # only when it is true.
    allow_background: bool = False
    # C20. The seat the background-agent tools act through and the conversation they act on. Both
    # None (the tools are hidden) unless the profile opted in AND the deployment wired a seat.
    background: BackgroundAgents | None = None
    background_owner: BackgroundOwner | None = None
