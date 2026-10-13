"""Port: ToolJournal - which tool calls of a turn began, and which of them ended (C30, D65).

Phase:   Agent-comms v2, wave 6
Adapter: adapters/driven/persistence_pg/tool_journal.py
Per-vertical: NO

One question, asked before a tool that is not `replay: safe` runs: "has THIS call (this turn, this
provider tool-call id) been started before?". After a crash the durable workflow runs the turn
step again; a call whose id is already journaled began in an earlier attempt, and the runner does
not run it a second time (`domain/tool_replay.py`).

The id the runner passes is the PROVIDER's `tool_call_id` qualified by the model-response ordinal
(`j<version>:r<n>:<id>`; the version guards the way `<n>` is counted), because some providers
reuse an id in a later response of the same turn. The
qualified id is stable where it matters: a call that was approved, and is executed by the resumed
turn, rebuilds the same history and so keeps the key it was suspended under. A fresh model
generation after a restart issues new ids, and those are new calls by definition.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from agent_core.domain.turn import TurnId

__all__ = ["ToolJournal", "ToolJournalUnavailableError", "ToolRunState"]


class ToolRunState(StrEnum):
    """What the journal knew about a call BEFORE this attempt wrote to it."""

    FRESH = "fresh"  # never seen: this attempt is the first
    STARTED = "started"  # begun by an earlier attempt, no end recorded
    FINISHED = "finished"  # begun and ended by an earlier attempt


class ToolJournalUnavailableError(RuntimeError):
    """The journal's store could not be reached (a pool checkout timed out, the database is down).

    Raised by every `ToolJournal` method. The runner treats it as follows: from `begin` it FAILS
    CLOSED (the tool is not run, because without the journal a repeat could not be told from a
    first run; the turn step fails and the workflow retries it); from `finish` and `abandon` it is
    logged and the result is kept (the call already happened; the row stays `started`, which a
    replay reads as interrupted).
    """


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
