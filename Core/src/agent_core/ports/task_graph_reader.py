"""Port: TaskGraphReader - the asks recorded under a turn a person started (C23, D54).

Phase:   Agent-comms v2, wave 3
Adapter: adapters/driven/peers/task_graph.py
Per-vertical: NO

One question: "which asks hang from this root turn, and how are they doing". The tenant predicate
is the guarantee, in the query, the way it is for `TranscriptReader`: a caller holding another
tenant's root id gets an empty tuple, indistinguishable from a turn that asked nobody.
"""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.task_graph import AskWindow

__all__ = ["TaskGraphReader"]


class TaskGraphReader(Protocol):
    async def asks_under(self, tenant_id: str, root_turn_id: str, *, limit: int = 500) -> AskWindow:
        """Every ask whose root turn is `root_turn_id`, direct and nested, background included,
        oldest first, at most `limit`, for that tenant only. Asks past the limit are counted in
        `AskWindow.omitted`, never dropped silently."""
        ...

    async def count_open(self, tenant_id: str, root_turn_id: str) -> int:
        """How many asks under the root are not finished (queued or running; neither answered,
        failed nor cancelled), counted over ALL of them, not over a window. The feed settles on
        this when `asks_under` was truncated, so a long-lived tree can still end."""
        ...
