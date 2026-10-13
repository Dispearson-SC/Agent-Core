"""Port: ToolJournal - which tool calls of a turn began, and which of them ended (C30, D65).

Phase:   Agent-comms v2, wave 6
Adapter: adapters/driven/persistence_pg/tool_journal.py
Per-vertical: NO

One question, asked before a tool that is not `replay: safe` runs: "has THIS call (this turn, this
provider tool-call id) been started before?". After a crash the durable workflow runs the turn
step again; a call whose id is already journaled began in an earlier attempt, and the runner does
not run it a second time (`domain/tool_replay.py`).

The id is the PROVIDER's `tool_call_id`, which is stable where it matters: a call that was
approved, and is executed by the resumed turn, keeps the id it was suspended under. A fresh model
generation after a restart issues new ids, and those are new calls by definition.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from agent_core.domain.turn import TurnId

__all__ = ["ToolJournal", "ToolRunState"]


class ToolRunState(StrEnum):
    """What the journal knew about a call BEFORE this attempt wrote to it."""

    FRESH = "fresh"  # never seen: this attempt is the first
    STARTED = "started"  # begun by an earlier attempt, no end recorded
    FINISHED = "finished"  # begun and ended by an earlier attempt


class ToolJournal(Protocol):
    async def begin(self, turn_id: TurnId, tool_call_id: str, tool: str) -> ToolRunState:
        """Record the call as started, atomically, and say what was there before. A call already
        journaled is left as it was."""
        ...

    async def finish(self, turn_id: TurnId, tool_call_id: str) -> None:
        """Record that the call ended (returned or raised). Idempotent."""
        ...

    async def abandon(self, turn_id: TurnId, tool_call_id: str) -> None:
        """Forget a call that began but never ran its body (it was deferred or suspended), so the
        run that executes it later finds it fresh. Idempotent."""
        ...
